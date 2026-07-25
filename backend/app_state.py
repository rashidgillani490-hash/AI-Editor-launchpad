# backend/app_state.py
import threading

class AppState:
    def __init__(self):
        self._lock = threading.Lock()
        self.active_buffers = {}
        self.terminal_processes = {}
        self.current_directory = None

    def update_file_buffer(self, file_id: str, content: str):
        with self._lock:
            self.active_buffers[file_id] = content

    def get_file_buffer(self, file_id: str):
        with self._lock:
            return self.active_buffers.get(file_id, None)
