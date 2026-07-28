"""
Command Interceptor Module for NexCore IDE

Intercepts user commands like "pip install" typed in the editor/console
and routes them to LibraryManager for installation without leaving the IDE.

Philosophy:
  - Seamless UX: Users can install packages without opening terminal
  - Non-blocking: IDE stays responsive during long downloads
  - Feedback-driven: Progress bar, status messages, error handling
  - Smart parsing: Understands pip syntax variations
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional, Callable, List, Tuple
from enum import Enum
from library_manager import LibraryManager, InstallationEvent

logger = logging.getLogger(__name__)


class CommandType(Enum):
    """Types of package management commands recognized."""
    PIP_INSTALL = "pip_install"
    PIP_UNINSTALL = "pip_uninstall"
    PIP_UPGRADE = "pip_upgrade"
    UNKNOWN = "unknown"


@dataclass
class ParsedCommand:
    """
    Parsed representation of a pip command.
    
    Attributes:
        command_type: Type of command (install, uninstall, upgrade)
        package_name: Package to operate on
        version: Version specifier if provided (e.g., "2.28.0")
        options: Dict of flags (e.g., {"upgrade": True})
        raw_command: Original text user typed
    """
    command_type: CommandType
    package_name: str
    version: Optional[str] = None
    options: dict = None
    raw_command: str = ""


class CommandParser:
    """
    Parses pip commands from user input.
    
    Supports:
      - pip install numpy
      - pip install numpy==1.24.3
      - pip install numpy>=1.0,<2.0
      - pip install --upgrade requests
      - pip install -r requirements.txt
      - pip uninstall pandas
    """
    
    # Regex patterns for different pip commands
    PIP_INSTALL_PATTERN = r'^\s*pip\s+install\s+(.+?)(?:\s*$|#)'
    PIP_UNINSTALL_PATTERN = r'^\s*pip\s+uninstall\s+(.+?)(?:\s*$|#)'
    PIP_UPGRADE_PATTERN = r'^\s*pip\s+install\s+--upgrade\s+(.+?)(?:\s*$|#)'
    
    @staticmethod
    def parse(command: str) -> Optional[ParsedCommand]:
        """
        Parse a pip command string.
        
        Args:
            command: User-typed command (e.g., "pip install numpy==1.24.3")
        
        Returns:
            ParsedCommand object, or None if not a recognized pip command.
            
        Why separate parser? Easier to test, reuse in multiple contexts
        (console, file watcher, REPL).
        """
        command = command.strip()
        
        # Try each pattern
        # Check upgrade first (it's more specific than install)
        if match := re.match(CommandParser.PIP_UPGRADE_PATTERN, command):
            package_spec = match.group(1).strip()
            pkg_name, version = CommandParser._parse_package_spec(package_spec)
            
            return ParsedCommand(
                command_type=CommandType.PIP_UPGRADE,
                package_name=pkg_name,
                version=version,
                options={"upgrade": True},
                raw_command=command
            )
        
        # Check install
        if match := re.match(CommandParser.PIP_INSTALL_PATTERN, command):
            package_spec = match.group(1).strip()
            
            # Handle requirements file
            if package_spec.startswith("-r "):
                return ParsedCommand(
                    command_type=CommandType.PIP_INSTALL,
                    package_name=package_spec[3:].strip(),
                    options={"requirements": True},
                    raw_command=command
                )
            
            pkg_name, version = CommandParser._parse_package_spec(package_spec)
            
            return ParsedCommand(
                command_type=CommandType.PIP_INSTALL,
                package_name=pkg_name,
                version=version,
                raw_command=command
            )
        
        # Check uninstall
        if match := re.match(CommandParser.PIP_UNINSTALL_PATTERN, command):
            package_spec = match.group(1).strip()
            pkg_name, version = CommandParser._parse_package_spec(package_spec)
            
            return ParsedCommand(
                command_type=CommandType.PIP_UNINSTALL,
                package_name=pkg_name,
                version=version,
                raw_command=command
            )
        
        return None
    
    @staticmethod
    def _parse_package_spec(spec: str) -> Tuple[str, Optional[str]]:
        """
        Extract package name and version from spec.
        
        Examples:
          "numpy" → ("numpy", None)
          "numpy==1.24.3" → ("numpy", "1.24.3")
          "numpy>=1.0,<2.0" → ("numpy", ">=1.0,<2.0")
          "requests==2.28.0 --no-deps" → ("requests", "2.28.0")
        """
        # Remove any flags (--no-deps, -i, etc.)
        spec = re.sub(r'\s+--?[a-zA-Z\-]+.*', '', spec).strip()
        
        # Split on version operators
        for op in ['==', '>=', '<=', '>', '<', '~=', '!=']:
            if op in spec:
                name, version = spec.split(op, 1)
                return name.strip(), (op + version.strip())
        
        # No version specified
        return spec.strip(), None


class CommandInterceptor:
    """
    Intercepts pip commands in the IDE and executes them via LibraryManager.
    
    Why a class? Maintains state (callbacks, manager instance) and allows
    IDE to hook in easily: just call interceptor.execute_command(user_input)
    """
    
    def __init__(self, library_manager: Optional[LibraryManager] = None):
        """
        Initialize the command interceptor.
        
        Args:
            library_manager: LibraryManager instance. If None, creates a new one.
                            
        Why injectable? Tests can mock LibraryManager.
        """
        self.manager = library_manager or LibraryManager()
        self.command_callbacks: List[Callable[[ParsedCommand, InstallationEvent], None]] = []
    
    def register_callback(self, callback: Callable) -> None:
        """
        Register a callback for command execution events.
        
        Args:
            callback: Function(command: ParsedCommand, event: InstallationEvent)
                     Called on every status update during execution.
        """
        self.command_callbacks.append(callback)
    
    def _notify_callbacks(self, command: ParsedCommand, event: InstallationEvent) -> None:
        """Internal: notify all callbacks of execution event."""
        for callback in self.command_callbacks:
            try:
                callback(command, event)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def execute_command(self, user_input: str) -> bool:
        """
        Parse and execute a pip command from user input.
        
        Args:
            user_input: Raw text user typed (e.g., "pip install numpy")
        
        Returns:
            True if command recognized and execution started, False otherwise.
            
        Why bool return? IDE can show "command not recognized" if False.
        """
        parsed = CommandParser.parse(user_input)
        
        if not parsed:
            return False
        
        logger.info(f"Intercepted command: {parsed.command_type.value}")
        
        # Route to appropriate handler
        if parsed.command_type == CommandType.PIP_INSTALL:
            if parsed.options and parsed.options.get("requirements"):
                # Installing from requirements.txt
                self.manager.install_from_requirements(parsed.package_name)
            else:
                # Installing a single package
                # Register callback to get progress updates
                self.manager.register_install_callback(
                    lambda event: self._notify_callbacks(parsed, event)
                )
                self.manager.install_package(
                    parsed.package_name,
                    version=parsed.version,
                    upgrade=False
                )
        
        elif parsed.command_type == CommandType.PIP_UPGRADE:
            self.manager.register_install_callback(
                lambda event: self._notify_callbacks(parsed, event)
            )
            self.manager.install_package(
                parsed.package_name,
                version=parsed.version,
                upgrade=True
            )
        
        elif parsed.command_type == CommandType.PIP_UNINSTALL:
            # Uninstall is synchronous, wrap in callback for consistency
            success = self.manager.uninstall_package(parsed.package_name)
            
            from library_manager import InstallStatus
            event = InstallationEvent(
                package_name=parsed.package_name,
                status=InstallStatus.SUCCESS if success else InstallStatus.FAILED,
                message="Package uninstalled" if success else "Failed to uninstall"
            )
            self._notify_callbacks(parsed, event)
        
        return True
    
    def is_pip_command(self, text: str) -> bool:
        """Quick check if text looks like a pip command (for syntax highlighting)."""
        return bool(CommandParser.parse(text))


# ============================================================================
# IDE Integration Helper: How to use this in your Tkinter GUI
# ============================================================================

class IDEIntegrationHelper:
    """
    Helper showing how to integrate CommandInterceptor with Tkinter editor.
    
    This is PSEUDO-CODE showing the pattern your IDE should follow.
    """
    
    @staticmethod
    def example_tkinter_integration():
        """
        Example: How to hook CommandInterceptor into a Tkinter Text widget
        
        In your main IDE window, you'd do something like:
        
        ```python
        from command_interceptor import CommandInterceptor
        
        class NexCoreIDE:
            def __init__(self, root):
                self.interceptor = CommandInterceptor()
                
                # Register callback to show progress in UI
                self.interceptor.register_callback(self.on_command_event)
                
                # Create editor text widget
                self.editor = tk.Text(root)
                self.editor.bind('<Return>', self.on_enter_key)
            
            def on_enter_key(self, event):
                '''User pressed Enter - check if it's a pip command'''
                line = self.editor.get("insert linestart", "insert lineend")
                
                # Try to execute as pip command
                if self.interceptor.execute_command(line):
                    # It was a pip command - show UI feedback
                    self.show_status("Installing package...")
                    return "break"  # Don't insert newline
                else:
                    # Normal line - insert newline
                    return None
            
            def on_command_event(self, command, event):
                '''Callback: installation progress update'''
                self.status_label.config(
                    text=f"{event.status.value}: {event.message}"
                )
                
                if event.progress_percent >= 0:
                    self.progress_bar.set(event.progress_percent / 100)
        ```
        """
        pass


# ============================================================================
# Demo Block
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    print("=" * 70)
    print("Demo: Command Interceptor for NexCore IDE")
    print("=" * 70)
    
    # Demo 1: Parse various pip commands
    print("\n1. Command Parsing Examples:")
    print("-" * 70)
    
    test_commands = [
        "pip install numpy",
        "pip install numpy==1.24.3",
        "pip install requests>=2.0,<3.0",
        "pip install --upgrade pandas",
        "pip uninstall scipy",
        "pip install -r requirements.txt",
        "not a pip command",
        "echo hello",
    ]
    
    parser = CommandParser()
    for cmd in test_commands:
        parsed = parser.parse(cmd)
        if parsed:
            status = f"✓ {parsed.command_type.value}: {parsed.package_name}"
            if parsed.version:
                status += f" v{parsed.version}"
            print(f"   {cmd:40s} → {status}")
        else:
            print(f"   {cmd:40s} → ✗ Not recognized")
    
    # Demo 2: Command interception with callbacks
    print("\n2. Command Execution (Simulated):")
    print("-" * 70)
    
    def on_command_event(command: ParsedCommand, event: InstallationEvent):
        """Callback that prints installation events."""
        status_bar = f"[{event.status.value.upper()}]"
        progress = f"({event.progress_percent}%)" if event.progress_percent >= 0 else ""
        print(f"   {status_bar} {event.message} {progress}")
    
    # Note: This would actually try to install packages if uncommented
    # For demo, we just show the parsing
    interceptor = CommandInterceptor()
    interceptor.register_callback(on_command_event)
    
    print("   Registered callback for command events")
    print("   (Actual installation would start if 'pip install numpy' typed)")
    
    # Demo 3: Check if text is pip command
    print("\n3. Quick Command Detection:")
    print("-" * 70)
    
    test_lines = [
        "pip install flask",
        "print('hello')",
        "# pip install should not work here",
        "pip uninstall django",
    ]
    
    for line in test_lines:
        is_pip = interceptor.is_pip_command(line)
        status = "✓ Would intercept" if is_pip else "✗ Would not intercept"
        print(f"   {line:35s} {status}")
    
    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print("\nIntegration Pattern:")
    print("  1. User types: pip install numpy")
    print("  2. IDE detects Enter key")
    print("  3. Calls: interceptor.execute_command(line)")
    print("  4. CommandParser extracts: name='numpy', version=None")
    print("  5. LibraryManager.install_package() runs in background")
    print("  6. Progress callbacks update IDE UI (progress bar, status)")
    print("  7. User sees visual feedback without leaving editor")
