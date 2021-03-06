#!/usr/bin/python2.7
#
# This program continuously one-way synchronises local directories
# into a remote machine.
#
# LocalClient ==> RemoteServer
#
# Usage:
#
#

#########################################################
# Imports
#########################################################
import argparse
import base64
import copy # copy.deepcopy(x)
import datetime
import getpass
import hashlib
import json
import os
import os.path
import re
import socket
import struct
import sys
import threading
import time



#########################################################
# Functions
#########################################################
def parse_args():
  parser = argparse.ArgumentParser(description='Synchronise a dir remotely.')
  parser.add_argument(
      '-m',
      '--mode',
      required=True,
      type=str,
      choices=('remote', 'local'),
      help='Mode to run this script in.',
  )

  parser.add_argument(
      '-p',
      '--port',
      default=8082,
      type=int,
      help='Remote server listen port.',
  )

  parser.add_argument(
      '-v',
      '--verbosity',
      default=2,
      type=int,
      help='Remote server listen port. levels=[{}]'.format(
          ', '.join(['{}={}'.format(LOG_LEVELS[i], i) \
              for i in range(len(LOG_LEVELS))])),
  )

  parser.add_argument(
      '-r',
      '--remote',
      default='localhost',
      type=str,
      help='Remote machine to connect to.',
  )

  parser.add_argument(
      '-d',
      '--dirs',
      required=True,
      type=str,
      nargs='+',
      help='Directories to keep in sync.',
  )

  parser.add_argument(
      '-s',
      '--shutdown_secs',
      type=int,
      default=12 * 60 * 60,
      help='Seconds for the server to auto-shutdown.',
  )

  parser.add_argument(
      '-t',
      '--token',
      type=str,
      default=None,
      help='Token used to sign network messages.',
  )

  parser.add_argument(
      '-i',
      '--ip_version',
      type=int,
      default=4,
      choices=(4, 6),
      help='What IP version to use.',
  )

  args = parser.parse_args()
  return args


def md5(*args):
  md5_hash = hashlib.md5()
  for arg in args:
    md5_hash.update(arg)
  return md5_hash.hexdigest()


def read_token(args_token):
  prompt_msg = 'Please type the token for the communication: '
  if args_token:
    token = args_token
  elif sys.stdin.isatty():
    token = getpass.getpass(prompt_msg)
  else:
    print(prompt_msg)
    token = sys.stdin.readline()
  print('The token is: ' + token)
  MIN_CHARS = 8
  if not token or len(token) < MIN_CHARS:
    msg = 'ERROR: A token of at least [{}] characters must be provided.'\
        .format(MIN_CHARS)
    raise HumaReadbleException(msg)
  return token


def create_socket(ip_version):
  if 4 == ip_version:
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  elif 6 == ip_version:
    return socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
  else:
    raise Exception('Unknown IP version: [{}].'.format(ip_version))


#########################################################
# Common Classes
#########################################################
class Logger(object):
  LEVEL = 3

  def __init__(self, log_name):
    self._name = log_name

  def debug(self, msg):
    self._log(3, msg)

  def info(self, msg):
    self._log(2, msg)

  def warn(self, msg):
    self._log(1, msg)

  def error(self, msg):
    self._log(0, msg)

  def _log(self, level, msg):
    if level > Logger.LEVEL:
      return

    ts = datetime.datetime.fromtimestamp(time.time()) \
        .strftime('%Y-%m-%d %H:%M:%S.%f')
    if level >= 0 and level < len(LOG_LEVELS):
      level = LOG_LEVELS[level].upper()[0]
    stream = sys.stdout
    stream.write('[{}][{}]<{}> {}\n'.format(level, ts, self._name, msg))
    stream.flush()


class HumaReadbleException(Exception):
  def __init__(self, msg):
    Exception.__init__(self, msg)
    self.msg = msg


class AutoShutdown(object):
  def __init__(self, seconds):
    self.log = Logger(type(self).__name__)
    self._seconds = seconds

  def _auto_shutdown(self):
    msg = ('Auto shutdown has been triggered after [{}] seconds. '
        'The program is exiting.'.format(self._seconds))
    self.log.error(msg)
    print(msg)
    # Pretty lazy/dirty/ugly way of doing this.
    os._exit(42)

  def __enter__(self):
    self._timer = threading.Timer(self._seconds, self._auto_shutdown)
    self._timer.start()
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    self._timer.cancel()
    self._timer = None


class StreamHandler(object):
  def __init__(self, token, socket):
    self.log = Logger(type(self).__name__)
    self._socket = socket
    self._buffer = ''
    self._serde = MessageSerde(token)

  def __enter__(self):
    self.log.debug('Entering...')
    return self

  def recvMessage(self):
    self.log.debug('Receiving message...')
    while True:
      data = self._socket.recv(BUFFER_SIZE_BYTES)
      datal = len(data)
      if datal == 0:
        msg = 'Remote client disconnected.'
        self.log.debug(msg)
        raise socket.error(msg)
      elif datal > 0:
        self.log.debug('Received [{}] bytes.'.format(datal))
        self._buffer += data
        message, unused = self._serde.deserialise(self._buffer)
        unused_bytes = len(unused)
        used_bytes = len(self._buffer) - unused_bytes
        self._buffer = unused
        if message == None:
          continue
        self.log.debug(
            'Received message_type=[{}] used_bytes=[{}] unused_bytes=[{}].'\
                .format(message.type_str(), used_bytes, unused_bytes))
        return message
      else:
        assert False, 'Should never get here!!! recv_bytes=[{}]'.format(datal)

  def __exit__(self, exc_type, exc_value, traceback):
    self.log.debug('Exiting...')
    if self._socket:
      self._socket.close()
      self._socket = None

  def sendMessage(self, message):
    data = self._serde.serialise(message)
    self.log.debug('Sending message of type [{}] and size [{}] bytes...'\
        .format(message.type_str(), len(data)))
    self._socket.sendall(data)


class MessageType(object):
  """ All Response types must be odd numbered """
  PING_REQUEST = 0
  PING_RESPONSE = 1
  DIFF_REQUEST = 2
  DIFF_RESPONSE = 3
  UPLOAD_REQUEST = 4
  UPLOAD_RESPONSE = 5

  @staticmethod
  def to_str(type_int):
    assert type(type_int) == int, type_int
    if type_int == MessageType.PING_REQUEST:
      return 'PING_REQUEST'
    elif type_int == MessageType.PING_RESPONSE:
      return 'PING_RESPONSE'
    elif type_int == MessageType.DIFF_REQUEST:
      return 'DIFF_REQUEST'
    elif type_int == MessageType.DIFF_RESPONSE:
      return 'DIFF_RESPONSE'
    elif type_int == MessageType.UPLOAD_REQUEST:
      return 'UPLOAD_REQUEST'
    elif type_int == MessageType.UPLOAD_RESPONSE:
      return 'UPLOAD_RESPONSE'
    else:
      return 'UNKNOWN'

  @staticmethod
  def to_pretty_str(type_int):
    return '{}({})'.format(MessageType.to_str(type_int), type_int)


class Message(object):
  def __init__(self, message_type):
    self.type = message_type
    self.body = {}
    self.body['ts'] = time.time()

  def __str__(self):
    return 'Message(type=[{}] body=[{}])'\
        .format(self.type_str(), json.dumps(self.body))

  def type_str(self):
    return MessageType.to_pretty_str(self.type)


class MessageSerde(object):
  def __init__(self, token):
    self.log = Logger(type(self).__name__)
    self._token = token

  def serialise(self, message):
    """ Returns a list of bytes containing the serialised msg """
    json_body = json.dumps(message.body)
    body_md5 = self._md5(json_body)
    body_bytes = len(json_body)
    header = struct.pack('>i32si', message.type, body_md5, body_bytes)
    return header + json_body

  def deserialise(self, input):
    """ Returns a tuple (Message, UnusedBytesList) """
    self.log.debug('Deserialising input of [{}] bytes...'.format(len(input)))
    header_bytes = 4 + 32 + 4
    if len(input) < header_bytes:
      return (None, input)
    msg_type, body_md5, body_bytes = \
        struct.unpack('>i32si', input[0:header_bytes])
    total_bytes = header_bytes + body_bytes
    if len(input) < total_bytes:
      return (None, input)
    json_body = input[header_bytes:total_bytes]
    expected_md5 = self._md5(json_body)
    if body_md5 != expected_md5:
      err = 'Server aborting! Expected_MD5=[{}] Actual_MD5=[{}]'\
          .format(expected_md5, body_md5)
      self.log.error(err)
      raise HumaReadbleException(err)
    message = Message(msg_type)
    message.body.update(json.loads(json_body))
    return (message, input[total_bytes:])

  def _md5(self, data):
    return md5(os.getenv('USER'), data, self._token)


class DirCrawler(object):
  def __init__(self, root_dir, exclude_list=[]):
    self.log = Logger(type(self).__name__)
    self._dir = root_dir
    self._dir = os.path.expanduser(self._dir)
    self._dir = os.path.abspath(self._dir)
    assert os.path.isdir(self._dir), \
        'Argument root_dir [{}] => [{}] must exist.'.format(root_dir, self._dir)
    self._excludes = [re.compile(pattern) for pattern in exclude_list]

  def crawl(self):
    '''Returns a list of relative paths of all files recursively.'''
    self.log.debug('Starting to crawl [{}]...'.format(self._dir))
    all_files = []
    for root, dirs, files in os.walk(self._dir):
      for f in files:
        complete_path = os.path.join(root, f)
        rel_path = os.path.relpath(complete_path, self._dir)
        if not self._is_excluded(rel_path):
          assert not os.path.isabs(rel_path), rel_path
          all_files.append(rel_path)
    self.log.debug(
        'Crawl found a total of [{}] files...'.format(len(all_files)))
    return all_files

  def crawl_and_hash(self, previous_results={}):
    '''Returns a dict with keyed off file_rel_path with md5_hash information.

    Each dict key refers to the relative path of a file.
    Each dict value contains a tuple with two elements:
    1. Epoch modified time.
    2. MD5 hash of the contents of the file.
    '''
    all_files = self.crawl()
    self.log.debug('Computing the md5 hash for [{}] files...'\
        .format(len(all_files)))
    data = {}
    computed_md5s = 0
    reused_md5s = 0
    for rel_path in all_files:
      abs_path = os.path.join(self._dir, rel_path)
      mtime = os.path.getmtime(abs_path)
      if rel_path in previous_results and \
          previous_results[rel_path][0] >= mtime:
        reused_md5s += 1
        data[rel_path] = previous_results[rel_path]
      else:
        md5 = DirCrawler.md5_hash(abs_path)
        computed_md5s += 1
        data[rel_path] = (mtime, md5)
    self.log.info('Finished computing all [{}] md5s and reused [{}].'.format(
        computed_md5s, reused_md5s))
    return data

  @staticmethod
  def md5_hash(file_path):
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
      for fragment in iter(lambda: f.read(BUFFER_SIZE_BYTES), b''):
        md5_hash.update(fragment)
    return md5_hash.hexdigest()

  def _is_excluded(self, path):
    for regex in self._excludes:
      if None != regex.match(path):
        # print path
        return True
    return False


class DirMonitor(object):
  def __init__(self, root_dirs):
    self.log = Logger(type(self).__name__)
    self.dirs = root_dirs
    self._crawlers = []
    for root in root_dirs:
      self._crawlers.append(DirCrawler(root))
    self.files = [list() for i in range(len(self._crawlers))]
    self._crawl_all()

  def get_dirs(self):
    return self.dirs

  def get_files(self):
    return self.files

  def start_monitoring(self):
    self._thread = threading.Thread(
        target=self._thread_main, name='DirMonitorThread')
    self._thread.daemon = True
    self._is_monitoring = True
    self._thread.start()
    return self

  def stop_monitoring(self):
    self._is_monitoring = False
    if self._thread:
      # self._thread.join()
      self._thread = None
    return self

  def _thread_main(self):
    self.log.info('Monitoring thread is running...')
    while self._is_monitoring:
      self.log.info('Monitor knows of [{}] files.'.format(len(self.files)))
      self._crawl_all()
      time.sleep(5.0)
    self.log.info('Monitoring thread is exiting.')

  def _crawl_all(self):
    files = []
    for i in range(len(self._crawlers)):
      crawler = self._crawlers[i]
      previous = self.files[i]
      files.append(crawler.crawl_and_hash(previous))
    self.files = files


class StateDiffer(object):
  def __init__(self):
    pass

  def diff(self, src, dst):
    '''Returns all files from src that need to be uploaded to dst

    Both [src] and [dst] should be lists containing dict() with the same
    exact format as returned from the DirCrawler.crawl_and_hash() method.

    Returns a tuple. Each position contains the files that need to be uploaded
    for that same index in [src].
    '''
    # Always return no diff
    assert len(src) == len(dst), \
        ('Both local and remote need to be monitoring the same amount of '
            'dirs. local_dirs=[{}] remote_dirs=[{}]').format(len(src), len(dst))
    dir_count = len(src)
    results = []
    for i in range(dir_count):
      src_dir = src[i]
      dst_dir = dst[i]
      current_diff = []
      results.append(current_diff)
      for path, (mtime, md5_hash) in src_dir.items():
        assert not os.path.isabs(path), path
        if path not in dst_dir or md5_hash != dst_dir[path][1]:
          current_diff.append(path)
    return results



#########################################################
# Remote Server Classes
#########################################################
class RemoteServer(object):
  def __init__(self, args):
    self.log = Logger(type(self).__name__)
    self.log.debug('Initializing...')
    self._args = args
    self._monitor = DirMonitor(args.dirs)
    self._msg_handler = RemoteMessageHandler(self._monitor)

  def __enter__(self):
    self.log.debug('Entering...')
    self._monitor.start_monitoring()
    self._socket = create_socket(self._args.ip_version)
    self._socket.bind(('', self._args.port))
    return self

  def run(self):
    self.log.debug('Running...')
    while True:
      self.log.info('Listening for incoming connections in port [{}]...'.format(
          self._args.port))
      self._socket.listen(1)
      connection, address = self._socket.accept()
      connection.settimeout(SOCKET_TIMEOUT_SECS)
      self.log.info('Accepted connection from address: [{}]'.format(
          str(address)))
      with StreamHandler(self._args.token, connection) as streamHandler:
        while True:
          try:
            request = streamHandler.recvMessage()
            response = self._msg_handler.handle_message(request)
            assert response.type % 2 == 1, \
                ('All responses must be of an odd type. '
                    'Found type [{}] instead.').format(response.type_str())
            streamHandler.sendMessage(response)
          except socket.timeout:
            self.log.warn('Socket timed out. Closing the connection.')
            break
          except socket.error:
            self.log.warn('Remote client disconneded. Closing the connection.')
            break

  def __exit__(self, exc_type, exc_value, traceback):
    self.log.debug('Exiting...')
    if exc_type and exc_value and traceback:
      self.log.error('Received exception type=[{}] value=[{}] traceback=[{}]'\
          .format(exc_type, exc_value, traceback))
    if self._socket:
      self._socket.close()
      self._socket = None
    if self._monitor:
      self._monitor.stop_monitoring()
      self._monitor = None


class FileWriter(object):
  def __init__(self, dirs):
    self.log = Logger(type(self).__name__)
    self._dirs = dirs

  def write(self, files):
    total_files = 0
    total_bytes = 0
    try:
      for i in range(len(files)):
        root = self._dirs[i]
        for rel_path, b64contents in files[i].items():
          contents = base64.b64decode(b64contents)
          self.log.debug('Writing [{}] bytes to root=[{}] file=[{}]...'\
              .format(len(contents), root, rel_path))
          path = os.path.join(root, rel_path)
          dirname = os.path.dirname(path)
          if not os.path.isdir(dirname):
            os.makedirs(dirname)
          with open(path, 'wb') as fp:
            fp.write(contents)
          total_files += 1
          total_bytes += len(contents)
    finally:
      self.log.info('Wrote a total of [{}] files and [{}] bytes.'\
          .format(total_files, total_bytes))


class RemoteMessageHandler(object):
  def __init__(self, monitor):
    self.log = Logger(type(self).__name__)
    self._monitor = monitor
    self._differ = StateDiffer()
    self._writer = FileWriter(self._monitor.get_dirs())

  def handle_message(self, req):
    resp = None
    self.log.info('RemoteMessageHandler received message of type [{}].'\
        .format(req.type_str()))
    # MessageType.PING_REQUEST
    if req.type == MessageType.PING_REQUEST:
      resp = Message(MessageType.PING_RESPONSE)
    # MessageType.DIFF_REQUEST
    elif req.type == MessageType.DIFF_REQUEST:
      resp = Message(MessageType.DIFF_RESPONSE)
      diff = self._differ.diff(req.body['files'], self._monitor.get_files())
      resp.body['diff'] = diff
    # MessageType.UPLOAD_REQUEST
    elif req.type == MessageType.UPLOAD_REQUEST:
      uploaded_files = req.body['uploaded_files']
      self._writer.write(uploaded_files)
      resp = Message(MessageType.UPLOAD_RESPONSE)
    else:
      err = ('No idea how to handle MessageType=[{}] so '
             'aborting connection.').format(message.type_str())
      self.log.error(err)
      raise error(err)
    self.log.info('Responding with MessageType=[{}].'.format(resp.type_str()))
    return resp


#########################################################
# Local Client Classes
#########################################################
class LocalClient(object):
  def __init__(self, args):
    self.log = Logger(type(self).__name__)
    self.log.debug('Initializing...')
    self._args = args
    self._socket = None

  def __enter__(self):
    self.log.debug('Entering...')
    self._monitor = DirMonitor(self._args.dirs)
    self._monitor.start_monitoring()
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    self.log.debug('Exiting...')
    self._disconnect()
    if self._monitor:
      self._monitor.stop_monitoring()
      self._monitor = None

  def run(self):
    self.log.debug('Running...')
    while True:
      try:
        self._connect()
        self._process_messages()
      except socket.timeout:
        self.log.warn('Socket timed out. Closing the connection.')
      except socket.error as exception:
        self.log.warn('Unexpected socket exception [{}]. Closing connection.'\
            .format(exception))
      finally:
        self._disconnect()
      time.sleep(1.0)

  def _connect(self):
    self._socket = create_socket(self._args.ip_version)
    self._socket.settimeout(SOCKET_TIMEOUT_SECS)
    remote = self._args.remote
    port = self._args.port
    self.log.info('Trying to connect to [{}:{}]'.format(remote, port))
    if self._args.ip_version == 4:
      self._socket.connect((remote, port))
    elif self._args.ip_version == 6:
      self._socket.connect((remote, port, 0, 0))
    else:
      raise Exception('Unknown IP version: [{}].'.format(ip_version))
    self.log.info('Successfully connected to [{}:{}]'.format(remote, port))

  def _process_messages(self):
    with StreamHandler(self._args.token, self._socket) as stream_handler:
      uploader = FileUploader(self._monitor, stream_handler)
      while True:
        uploader.upload_files()
        time.sleep(3.0)

  def _disconnect(self):
    if self._socket:
      self._socket.close()
      self._socket = None


class FileUploader(object):
  def __init__(self, monitor, stream_handler):
    self.log = Logger(type(self).__name__)
    self._monitor = monitor
    self._handler = stream_handler

  def upload_files(self):
    # DIFF_REQUEST
    diff_request = Message(MessageType.DIFF_REQUEST)
    diff_request.body['files'] = self._monitor.files
    self._handler.sendMessage(diff_request)
    diff_response = self._handler.recvMessage()
    files = diff_response.body['diff']
    self.log.info('A total of [{0}] files need to be uploaded.'\
        .format(len(files[0])))
    # UPLOAD_REQUEST
    upload_request = Message(MessageType.UPLOAD_REQUEST)
    upload_request.body['uploaded_files'] = self._files_to_upload(files)
    self._handler.sendMessage(upload_request)
    upload_response = self._handler.recvMessage()

  def _files_to_upload(self, files):
    results = []
    dirs = self._monitor.get_dirs()
    for dir_index in range(len(files)):
      local_root = dirs[dir_index]
      files_per_dir = files[dir_index]
      current = {}
      results.append(current)
      for rel_path in files_per_dir:
        assert not os.path.isabs(rel_path), rel_path
        abs_path = os.path.join(local_root, rel_path)
        with open(abs_path, 'r') as fp:
          content = fp.read()
        content = base64.b64encode(content)
        current[rel_path] = content
    return results



#########################################################
# Constants
#########################################################
SOCKET_TIMEOUT_SECS = 5.0
BUFFER_SIZE_BYTES = 1024 * 1024
LOG_LEVELS = ('error', 'warn', 'info', 'debug')
LOG = Logger('main')



#########################################################
# Main
#########################################################
def main():
  try:
    args = parse_args()
    with  AutoShutdown(args.shutdown_secs) as shutdown:
      args.token = read_token(args.token)
      Logger.LEVEL = args.verbosity
      LOG.info('Mode: [{}]'.format(args.mode))
      if args.mode == 'remote':
        with RemoteServer(args) as server:
          server.run()
      else:
        with LocalClient(args) as client:
          client.run()
  except HumaReadbleException, exception:
    print(exception.msg)


if __name__ == '__main__':
  main()

