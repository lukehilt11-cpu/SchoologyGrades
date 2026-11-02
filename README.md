# SchoologyGrades
An automated grade monitoring tool for Schoology. It signs in securely using Google Chrome via Selenium, retrieves your latest grades across all registered courses in parallel, and generates a clean, standalone HTML grade report with support for dark/light themes and custom grade scales.

## Installation \& Downloads

Pre-built binaries for **Windows** and **macOS** are available directly from the repository releases:

* **Download the Latest Release:** Go to the [GitHub Releases](https://github.com/lukehilt11-cpu/SchoologyGrades/releases/tag/latest) page.
* **Windows:** Download and run `SchoologyGrades-1.0-Setup.exe` to install, or use the portable `.zip` version.
* **macOS:** Download the `.app` bundle from the latest release.

> \*\*Note:\*\* Python is \*\*not required\*\* to use the pre-built installers or executables. Google Chrome must be installed on your computer.

## How It Works

1. **Setup:** Run `SchoologySetup.exe` (or open Setup on macOS) to enter your school login credentials and pick which classes you want to track.
2. **Check Grades:** Run `SchoologyGrades.exe` (or open Schoology Grades on macOS) whenever you want an update.
3. **Report:** The app fetches grades across all your selected courses and automatically opens a clean, responsive HTML report (`grade\_report.html`) complete with automatic dark and light theme switching.

## Features

* **Ultra-Fast Parallel Scraping:** Fetches grades across all courses concurrently using parallel browser tabs and optimized page probe scripts.
* **Smart Caching \& Direct Fetch:** Automatically verifies and uses standard HTTP fetch calls after initial setup, falling back seamlessly to full browser scraping when required.
* **Modern Responsive HTML Report:** Generates an easy-to-read HTML report complete with dark/light mode toggles.
* **Secure Local Storage:** Saves authentication profiles locally in the `data/` folder so you don't need to sign in every time.
* **Interactive Setup Wizard:** Built-in GUI tool to easily re-select courses or update credentials.

## Project Structure

```
.
├── SchoologyGrades.exe  # Standalone grade checker application executable
├── SchoologySetup.exe   # Standalone setup wizard executable
├── build.bat            # Windows build automation script
├── data/                # Local user configuration, logs, reports, and browser profile
├── releases/            # Output directory for installers and release archives
└── source/
    ├── grades.pyw       # Main application entry point
    ├── setup.py         # GUI setup tool for selecting courses \& credentials
    ├── sgcommon.py      # Shared utilities, Selenium helpers, \& error handling
    ├── make\_icons.py    # Icon asset generator
    ├── grades.spec      # PyInstaller specification file
    ├── installer.iss    # Inno Setup script for installer packaging
    ├── VERSION          # Current version tag
    └── icons/           # Application icon assets (.ico, .png, .icns, etc.)
```

## Building from Source (Developers Only)

If you prefer to run from source or build the executables yourself:

### Prerequisites

* Python 3.8+
* Google Chrome

### Running from Source

1. Install required dependencies:

```cmd
   pip install pyinstaller "selenium>=4.6" pillow numpy
   ```

2. Run Setup:

```cmd
   python source/setup.py
   ```

3. Run Grade Checker:

```cmd
   pythonw source/grades.pyw
   ```

### Building Executables (Windows)

Use the included `build.bat` script:

|Command|Description|
|-|-|
|`build.bat`|Performs a full build of applications and creates an installer in `releases/`.|
|`build.bat dev`|Builds only the standalone executables (`SchoologyGrades.exe` \& `SchoologySetup.exe`).|
|`build.bat force`|Forces a complete rebuild even if source files haven't changed.|
|`build.bat run`|Rebuilds (if necessary) and immediately launches the resulting program or installer.|

> \*\*Note:\*\* Building setup installers requires \*\*Inno Setup 6\*\* (`winget install JRSoftware.InnoSetup`). If missing, a portable `.zip` bundle is created in `releases/` instead.

## Advanced \& Troubleshooting

* **Multi-Factor Authentication (MFA):** If your school requires Microsoft MFA or initial interactive login approval, launch Setup and ensure **"Show the browser while signing in"** is checked.
* **App Translocation (macOS):** On macOS, ensure the app folder is located inside `\~/Documents` or `/Applications` to prevent macOS Security from running the app in isolated mode.
* **Resetting Configuration:** Re-run Setup at any time to update credentials or select different courses.

## License

No license specified. All rights reserved by the original authors.

