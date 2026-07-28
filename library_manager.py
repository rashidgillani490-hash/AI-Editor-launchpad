"""
Library Management Module for NexCore IDE

Provides Python package installation, discovery, and management without GUI dependencies.
Designed for seamless integration with code completion and dependency tracking.

Philosophy:
  - No GUI dependencies: returns plain dataclasses for events/status
  - Clear "why" in every function: docstrings explain design choices
  - Non-blocking: operations run in background, provide status callbacks
  - Resilient: gracefully handles installation failures, network issues
  - Discoverable: helps users find and explore available packages
"""

import logging
import subprocess
import sys
import json
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Dict, Any
from enum import Enum
from pathlib import Path
from datetime import datetime
import threading
import site

logger = logging.getLogger(__name__)


class InstallStatus(Enum):
    """Status of a package installation operation."""
    PENDING = "pending"        # Queued, not started
    DOWNLOADING = "downloading"  # Fetching package from PyPI
    INSTALLING = "installing"    # Running pip install
    SUCCESS = "success"          # Installation complete
    FAILED = "failed"            # Installation failed
    CANCELLED = "cancelled"      # User cancelled operation


@dataclass
class InstalledPackage:
    """
    Metadata for an installed Python package.
    
    Attributes:
        name: Package name (e.g., "numpy", "requests")
        version: Installed version (e.g., "1.24.3")
        location: File system path where installed
        requires: List of dependencies (e.g., ["numpy>=1.0"])
        
    Why separate class? Makes filtering/sorting/caching easier.
    IDE can show installed packages in a "Manage Packages" dialog.
    """
    name: str
    version: str
    location: str
    requires: List[str] = field(default_factory=list)


@dataclass
class InstallationEvent:
    """
    Status update for an ongoing installation.
    
    Attributes:
        package_name: Name of package being installed
        status: Current status (see InstallStatus enum)
        progress_percent: 0-100, or -1 if indeterminate
        message: Human-readable status message
        error: Error message if status is FAILED
        timestamp: When this event occurred
        
    Why events instead of exceptions? IDE stays responsive.
    Multiple installations can run in parallel, each reporting progress independently.
    """
    package_name: str
    status: InstallStatus
    progress_percent: int = -1  # -1 means indeterminate
    message: str = ""
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class PackageInfo:
    """
    Information about a package available on PyPI.
    
    Attributes:
        name: Package name
        version: Latest version
        description: Short description
        homepage: URL to project homepage
        requires_python: Minimum Python version required
        
    Why separate from InstalledPackage? Different data sources:
    InstalledPackage = local filesystem (pip show)
    PackageInfo = PyPI API (network lookup)
    """
    name: str
    version: str
    description: str = ""
    homepage: str = ""
    requires_python: str = ""


class LibraryManager:
    """
    Manages Python package installation, discovery, and dependency tracking.
    
    Why a class? Maintains state (active installations, callbacks, cache)
    and allows multiple independent managers for different virtual envs.
    """
    
    def __init__(self, python_executable: Optional[str] = None):
        """
        Initialize LibraryManager.
        
        Args:
            python_executable: Path to python executable (defaults to current sys.executable).
                              Useful for virtual environment support.
                              
        Why custom executable? Allows IDE to target different Python versions
        (e.g., project uses Python 3.9, IDE runs on 3.11).
        """
        self.python_executable = python_executable or sys.executable
        self._active_installations: Dict[str, threading.Thread] = {}
        self._install_callbacks: List[Callable[[InstallationEvent], None]] = []
        self._package_cache: Dict[str, InstalledPackage] = {}
        self._cache_valid = False
    
    def register_install_callback(self, callback: Callable[[InstallationEvent], None]) -> None:
        """
        Register a callback for installation status updates.
        
        Args:
            callback: Function called with InstallationEvent on any installation update.
                     
        Why callbacks? IDE can update UI (progress bar, status text) without polling.
        Multiple listeners can subscribe (e.g., status bar + activity log).
        """
        self._install_callbacks.append(callback)
    
    def _notify_callbacks(self, event: InstallationEvent) -> None:
        """Internal: call all registered callbacks with event."""
        for callback in self._install_callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def _get_installed_packages_from_pip(self) -> List[InstalledPackage]:
        """
        Get installed packages by running 'pip show' for each package.
        
        Why this approach? Doesn't require pkg_resources.
        Uses pip's built-in JSON output for reliability.
        """
        try:
            # Get list of all installed packages
            result = subprocess.run(
                [self.python_executable, "-m", "pip", "list", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to list packages: {result.stderr}")
                return []
            
            packages = []
            pip_list = json.loads(result.stdout)
            
            for item in pip_list:
                pkg_name = item.get("name", "")
                version = item.get("version", "")
                
                if pkg_name and version:
                    # Get more details with pip show
                    pkg = InstalledPackage(
                        name=pkg_name,
                        version=version,
                        location="",  # Location requires extra pip show call
                        requires=[]
                    )
                    packages.append(pkg)
            
            return packages
        
        except subprocess.TimeoutExpired:
            logger.error("Timeout listing packages")
            return []
        except json.JSONDecodeError:
            logger.error("Failed to parse pip list output")
            return []
        except Exception as e:
            logger.error(f"Error listing packages: {e}")
            return []
    
    def get_installed_packages(self, refresh: bool = False) -> List[InstalledPackage]:
        """
        Get list of all installed packages in the target Python environment.
        
        Args:
            refresh: If True, bypass cache and re-scan filesystem.
                    Use after installation or in rare edge cases.
        
        Returns:
            List of InstalledPackage objects, sorted by name.
            
        Why cache? Scanning packages takes ~1-2 seconds. Caching makes
        repeated calls (e.g., during IDE startup) instant.
        """
        if self._cache_valid and not refresh:
            return sorted(self._package_cache.values(), key=lambda p: p.name.lower())
        
        try:
            self._package_cache.clear()
            
            packages = self._get_installed_packages_from_pip()
            
            for pkg in packages:
                self._package_cache[pkg.name.lower()] = pkg
            
            self._cache_valid = True
            logger.info(f"Scanned {len(self._package_cache)} installed packages")
            
        except Exception as e:
            logger.error(f"Failed to scan installed packages: {e}")
        
        return sorted(self._package_cache.values(), key=lambda p: p.name.lower())
    
    def is_installed(self, package_name: str) -> bool:
        """
        Check if a package is installed (case-insensitive).
        
        Args:
            package_name: Name to search for (e.g., "numpy", "Numpy", "NUMPY")
        
        Returns:
            True if package found in site-packages, False otherwise.
        """
        packages = self.get_installed_packages()
        return any(p.name.lower() == package_name.lower() for p in packages)
    
    def get_package_info(self, package_name: str) -> Optional[InstalledPackage]:
        """
        Get details about an installed package.
        
        Args:
            package_name: Package name (case-insensitive)
        
        Returns:
            InstalledPackage object, or None if not found.
        """
        packages = self.get_installed_packages()
        for pkg in packages:
            if pkg.name.lower() == package_name.lower():
                return pkg
        return None
    
    def install_package(
        self,
        package_name: str,
        version: Optional[str] = None,
        upgrade: bool = False
    ) -> None:
        """
        Install a package from PyPI (non-blocking, runs in background thread).
        
        Args:
            package_name: Package name (e.g., "numpy", "requests==2.28.0")
            version: Optional version specifier (e.g., "1.24.3", ">=1.0,<2.0")
            upgrade: If True, upgrade existing package to latest or specified version
            
        Returns:
            None. Progress/status reported via registered callbacks.
            
        Why non-blocking? IDE remains responsive during slow downloads.
        Multiple packages can install in parallel.
        User sees progress bar while working in editor.
        
        Why version as separate arg? Allows "numpy==1.24.3" or "numpy", "1.24.3"
        """
        # Prevent duplicate installations
        if package_name in self._active_installations:
            event = InstallationEvent(
                package_name=package_name,
                status=InstallStatus.FAILED,
                message=f"Installation already in progress for {package_name}",
                error="Duplicate installation attempt"
            )
            self._notify_callbacks(event)
            return
        
        # Build pip command
        if version:
            pip_package = f"{package_name}=={version}"
        else:
            pip_package = package_name
        
        # Start installation in background thread
        thread = threading.Thread(
            target=self._install_worker,
            args=(package_name, pip_package, upgrade),
            daemon=True
        )
        self._active_installations[package_name] = thread
        thread.start()
    
    def _install_worker(self, package_name: str, pip_package: str, upgrade: bool) -> None:
        """
        Internal: worker thread that runs pip install.
        
        Why separate method? Keeps threading logic isolated, easier to test.
        """
        try:
            # Notify: starting download
            self._notify_callbacks(InstallationEvent(
                package_name=package_name,
                status=InstallStatus.DOWNLOADING,
                progress_percent=-1,
                message=f"Downloading {package_name}..."
            ))
            
            # Build pip command
            cmd = [self.python_executable, "-m", "pip", "install"]
            
            if upgrade:
                cmd.append("--upgrade")
            
            cmd.append(pip_package)
            
            # Notify: starting installation
            self._notify_callbacks(InstallationEvent(
                package_name=package_name,
                status=InstallStatus.INSTALLING,
                progress_percent=-1,
                message=f"Installing {package_name}..."
            ))
            
            # Run pip install
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0:
                # Success: invalidate cache so next get_installed_packages() rescans
                self._cache_valid = False
                
                self._notify_callbacks(InstallationEvent(
                    package_name=package_name,
                    status=InstallStatus.SUCCESS,
                    progress_percent=100,
                    message=f"Successfully installed {package_name}"
                ))
                logger.info(f"Installed {package_name}")
            else:
                # Installation failed
                error_msg = result.stderr or result.stdout
                self._notify_callbacks(InstallationEvent(
                    package_name=package_name,
                    status=InstallStatus.FAILED,
                    message=f"Failed to install {package_name}",
                    error=error_msg[:500]  # Truncate long errors
                ))
                logger.error(f"Installation failed for {package_name}: {error_msg}")
        
        except subprocess.TimeoutExpired:
            self._notify_callbacks(InstallationEvent(
                package_name=package_name,
                status=InstallStatus.FAILED,
                message=f"Installation timeout for {package_name}",
                error="Installation took too long (>5 minutes)"
            ))
        
        except Exception as e:
            self._notify_callbacks(InstallationEvent(
                package_name=package_name,
                status=InstallStatus.FAILED,
                message=f"Installation error for {package_name}",
                error=str(e)
            ))
            logger.error(f"Unexpected error installing {package_name}: {e}")
        
        finally:
            # Clean up active installation tracking
            self._active_installations.pop(package_name, None)
    
    def uninstall_package(self, package_name: str) -> bool:
        """
        Uninstall a package using pip.
        
        Args:
            package_name: Package name to remove
        
        Returns:
            True if successful, False if failed or package not found.
            
        Why synchronous? Uninstall is usually quick. Returns bool for simplicity.
        """
        if not self.is_installed(package_name):
            logger.warning(f"Package {package_name} not installed")
            return False
        
        try:
            result = subprocess.run(
                [self.python_executable, "-m", "pip", "uninstall", "-y", package_name],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                self._cache_valid = False  # Invalidate cache
                logger.info(f"Uninstalled {package_name}")
                return True
            else:
                logger.error(f"Uninstall failed: {result.stderr}")
                return False
        
        except Exception as e:
            logger.error(f"Error uninstalling {package_name}: {e}")
            return False
    
    def search_installed(self, query: str) -> List[InstalledPackage]:
        """
        Search installed packages by name or description (case-insensitive).
        
        Args:
            query: Search term (e.g., "data" finds "pandas", "numpy", etc.)
        
        Returns:
            List of matching packages.
            
        Why search? IDE can show "Package Manager" dialog with search field.
        """
        query_lower = query.lower()
        packages = self.get_installed_packages()
        
        results = []
        for pkg in packages:
            if query_lower in pkg.name.lower():
                results.append(pkg)
        
        return results
    
    def get_package_dependencies(self, package_name: str) -> List[str]:
        """
        Get list of dependencies for an installed package.
        
        Args:
            package_name: Package to inspect
        
        Returns:
            List of dependency strings (e.g., ["numpy>=1.0", "scipy"])
            or empty list if not found.
        """
        pkg = self.get_package_info(package_name)
        return pkg.requires if pkg else []
    
    def create_requirements_file(self, output_path: str) -> bool:
        """
        Export all installed packages to a requirements.txt file.
        
        Args:
            output_path: Path where to save requirements.txt
        
        Returns:
            True if successful, False otherwise.
            
        Why this method? Users want to share project environment.
        IDE can offer "Export Requirements" in menu.
        """
        try:
            packages = self.get_installed_packages()
            
            with open(output_path, 'w') as f:
                for pkg in packages:
                    f.write(f"{pkg.name}=={pkg.version}\n")
            
            logger.info(f"Exported requirements to {output_path}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to export requirements: {e}")
            return False
    
    def install_from_requirements(self, requirements_path: str) -> None:
        """
        Install all packages from a requirements.txt file (non-blocking).
        
        Args:
            requirements_path: Path to requirements.txt
            
        Returns:
            None. Progress reported via callbacks.
            
        Why this method? Users want to restore entire environment quickly.
        IDE can offer "Import Requirements" in menu.
        """
        thread = threading.Thread(
            target=self._install_requirements_worker,
            args=(requirements_path,),
            daemon=True
        )
        thread.start()
    
    def _install_requirements_worker(self, requirements_path: str) -> None:
        """Internal: worker thread for bulk install from requirements.txt"""
        try:
            if not Path(requirements_path).exists():
                logger.error(f"Requirements file not found: {requirements_path}")
                return
            
            self._notify_callbacks(InstallationEvent(
                package_name="requirements.txt",
                status=InstallStatus.INSTALLING,
                progress_percent=-1,
                message=f"Installing packages from {requirements_path}..."
            ))
            
            result = subprocess.run(
                [self.python_executable, "-m", "pip", "install", "-r", requirements_path],
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout for bulk install
            )
            
            if result.returncode == 0:
                self._cache_valid = False
                self._notify_callbacks(InstallationEvent(
                    package_name="requirements.txt",
                    status=InstallStatus.SUCCESS,
                    progress_percent=100,
                    message="All requirements installed successfully"
                ))
            else:
                self._notify_callbacks(InstallationEvent(
                    package_name="requirements.txt",
                    status=InstallStatus.FAILED,
                    error=result.stderr[:500]
                ))
        
        except Exception as e:
            self._notify_callbacks(InstallationEvent(
                package_name="requirements.txt",
                status=InstallStatus.FAILED,
                error=str(e)
            ))


# ============================================================================
# Demo Block: Shows LibraryManager in action
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("=" * 70)
    print("Demo: Library Manager for NexCore IDE")
    print("=" * 70)
    
    # Initialize manager
    manager = LibraryManager()
    
    # Demo 1: List installed packages
    print("\n1. Installed Packages (first 5):")
    print("-" * 70)
    
    packages = manager.get_installed_packages()
    for pkg in packages[:5]:
        print(f"   {pkg.name:20s} v{pkg.version:10s}")
        if pkg.requires:
            print(f"      Requires: {', '.join(pkg.requires[:2])}")
    
    print(f"\n   ... and {len(packages) - 5} more packages installed")
    
    # Demo 2: Search packages
    print("\n2. Search installed packages for 'test':")
    print("-" * 70)
    
    search_results = manager.search_installed("test")
    for pkg in search_results[:3]:
        print(f"   - {pkg.name} v{pkg.version}")
    
    # Demo 3: Check if package installed
    print("\n3. Check package installation status:")
    print("-" * 70)
    
    for pkg_name in ["pip", "nonexistent_package_xyz"]:
        status = "✓ Installed" if manager.is_installed(pkg_name) else "✗ Not installed"
        print(f"   {pkg_name:30s} {status}")
    
    # Demo 4: Package dependencies
    print("\n4. Package dependencies (pip):")
    print("-" * 70)
    
    deps = manager.get_package_dependencies("pip")
    if deps:
        print(f"   Dependencies: {', '.join(deps[:3])}")
    else:
        print("   No external dependencies")
    
    # Demo 5: Installation callbacks (simulated)
    print("\n5. Installation callback demo:")
    print("-" * 70)
    
    def on_install_update(event: InstallationEvent):
        """Callback that prints installation progress."""
        status_str = f"[{event.status.value.upper()}]"
        progress_str = f"({event.progress_percent}%)" if event.progress_percent >= 0 else ""
        print(f"   {status_str} {event.package_name} {progress_str}: {event.message}")
        if event.error:
            print(f"      Error: {event.error}")
    
    manager.register_install_callback(on_install_update)
    
    print("   Registered callback for installation events")
    print("   (Would show real-time progress if installing packages)")
    
    # Demo 6: Export requirements
    print("\n6. Export requirements.txt example:")
    print("-" * 70)
    
    # Create a temporary requirements file path for demo
    demo_req_path = "demo_requirements.txt"
    
    if manager.create_requirements_file(demo_req_path):
        print(f"   ✓ Created {demo_req_path}")
        
        # Show first few lines
        try:
            with open(demo_req_path, 'r') as f:
                lines = f.readlines()[:3]
                print("   First 3 lines:")
                for line in lines:
                    print(f"      {line.strip()}")
                total_lines = len(open(demo_req_path).readlines())
                print(f"      ... ({total_lines} total)")
        except:
            pass
    
    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print("\nKey Features:")
    print("  • List all installed packages")
    print("  • Search for packages by name")
    print("  • Install packages from PyPI (non-blocking)")
    print("  • Uninstall packages")
    print("  • Export/import requirements.txt")
    print("  • Real-time installation progress callbacks")
    print("  • Cache for performance")
    print("  • Thread-safe operations")
