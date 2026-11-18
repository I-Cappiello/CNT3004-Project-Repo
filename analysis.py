import csv
import threading
from pathlib import Path

class Metrics():
  # Creates a dictionary where the variables will be stored
  METRICS = [
    'timestamp_utc',
    'side',
    'session_id',
    'request_id',
    'client=user',
    'operation',
    'path',
    'bytes',
    'file_bytes',
    'offset',
    'transfer_s',
    'response_s',
    'status',
    'error',
    'trial',
    'load'
  ]

  # Metrics Class initializer
  def __init__(self, log):
    # Creates the directory for the log csv if it doesn't already exist
    self.path = Path(log)
    self.path.parent.mkdir(parents=True,exist_ok=True)

    # Prevents multiple threads from writing to the csv at once
    self.lock = threading.Lock()
    self.closed = False

    # Opens the csv file
    self.file = self.path.open(
      'a',
      newline='',
      encoding='utf-8'
    )

    # Writes the csv
    self.writer = csv.DictWriter(
      self.file,
      fieldnames=self.METRICS,
      extrasaction='ignore'
    )

    # If the CSV is empty, it writes the column names
    if self.path.stat().st_size == 0:
      self.writer.writeheader()
      self.file.flush()

    # Initializes counters
    self.total_requests = 0
    self.successful_requests = 0
    self.failed_requests = 0
    self.total_bytes = 0

  # Adds a row to the csv
  def record(self, **row):
    # Prevents multiple threads from writing at once
    with self.lock:
      # Checks if the file is closed
      if self.closed:
        raise RuntimeError('Metrics logger is closed')
      # Writes the row into the csv
      self.writer.writerow(row)
      self.file.flush()

      # Increments counters
      self.total_requests += 1
      self.total_bytes += row.get('bytes',0)
      if row.get('status') == 'ok':
        self.successful_requests += 1
      else:
        self.failed_requests += 1

  # Closes the CSV
  def close(self):
    with self.lock:
      if not self.closed:
        self.file.flush()
        self.file.close()
        self.closed = True
      
