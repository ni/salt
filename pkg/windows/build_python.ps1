<#
.SYNOPSIS
Script that builds Python from source using the Relative Environment for Python
project (relenv):

https://github.com/saltstack/relative-environment-for-python

.DESCRIPTION
This script builds python from Source. It then creates the directory structure
as created by the Python installer. This includes all header files, scripts,
dlls, library files, and pip.

.EXAMPLE
build_python.ps1 -PythonDir C:\Python310

#>
param(
    [Parameter(Mandatory=$false)]    
    # Directory of the Python installation to use
    [String] $PythonDir,
	
	[Parameter(Mandatory=$false)]    
    # Don't pretify the output of the Write-Result
    [Switch] $CICD
)

#-------------------------------------------------------------------------------
# Script Preferences
#-------------------------------------------------------------------------------

[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"
$ErrorActionPreference = "Stop"

if ( $Architecture -eq "amd64" ) {
  $Architecture = "x64"
}

#-------------------------------------------------------------------------------
# Script Functions
#-------------------------------------------------------------------------------

function Write-Result($result, $ForegroundColor="Green") {
    if ( $CICD ) {
        Write-Host $result -ForegroundColor $ForegroundColor
    } else {
        $position = 80 - $result.Length - [System.Console]::CursorLeft
        Write-Host -ForegroundColor $ForegroundColor ("{0,$position}$result" -f "")
    }}

#-------------------------------------------------------------------------------
# Start the Script
#-------------------------------------------------------------------------------

Write-Host "Running build python"

#-------------------------------------------------------------------------------
# Global Script Preferences
#-------------------------------------------------------------------------------
# The Python Build script doesn't disable the progress bar. This is a problem
# when trying to add this to CICD so we need to disable it system wide. This
# Adds $ProgressPreference=$false to the Default PowerShell profile so when the
# cpython build script is launched it will not display the progress bar. This
# file will be backed up if it already exists and will be restored at the end
# this script.
if ( Test-Path -Path "$profile" ) {
    if ( ! (Test-Path -Path "$profile.salt_bak") ) {
        Write-Host "Backing up PowerShell Profile: " -NoNewline
        Move-Item -Path "$profile" -Destination "$profile.salt_bak"
        if ( Test-Path -Path "$profile.salt_bak" ) {
            Write-Result "Success" -ForegroundColor Green
        } else {
            Write-Result "Failed" -ForegroundColor Red
            exit 1
        }
    }
}

$CREATED_POWERSHELL_PROFILE_DIRECTORY = $false
if ( ! (Test-Path -Path "$(Split-Path "$profile" -Parent)") ) {
    Write-Host "Creating WindowsPowerShell Directory: " -NoNewline
    New-Item -Path "$(Split-Path "$profile" -Parent)" -ItemType Directory | Out-Null
    if ( Test-Path -Path "$(Split-Path "$profile" -Parent)" ) {
        $CREATED_POWERSHELL_PROFILE_DIRECTORY = $true
        Write-Result "Success" -ForegroundColor Green
    } else {
        Write-Result "Failed" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Creating Temporary PowerShell Profile: " -NoNewline
'$ProgressPreference = "SilentlyContinue"' | Out-File -FilePath $profile
'$ErrorActionPreference = "Stop"' | Out-File -FilePath $profile
Write-Result "Success" -ForegroundColor Green

#-------------------------------------------------------------------------------
# Script Variables
#-------------------------------------------------------------------------------
$SCRIPT_DIR   = (Get-ChildItem "$($myInvocation.MyCommand.Definition)").DirectoryName
$BUILD_DIR    = "$SCRIPT_DIR\buildenv"
$RELENV_DIR   = "${env:LOCALAPPDATA}\relenv"
$SYS_PY_BIN   = (python -c "import sys; print(sys.executable)")
$BLD_PY_BIN   = "$BUILD_DIR\python.exe"

if ( $Architecture -eq "x64" ) {
    $ARCH         = "amd64"
} else {
    $ARCH         = "x86"
}

if ( $PythonDir ) {
	$PYTHON_DIR = $PythonDir
} else {
	# use System Python if no Python directory was specified
	$PYTHON_DIR = (python -c "import sys, os; print(os.path.dirname(sys.executable))")
}

#-------------------------------------------------------------------------------
# Prepping Environment
#-------------------------------------------------------------------------------
if ( Test-Path -Path "$BUILD_DIR" ) {
    Write-Host "Removing existing build directory: " -NoNewline
    Remove-Item -Path "$BUILD_DIR" -Recurse -Force
    if ( Test-Path -Path "$BUILD_DIR" ) {
        Write-Result "Failed" -ForegroundColor Red
        exit 1
    } else {
        Write-Result "Success" -ForegroundColor Green
    }
}

#-------------------------------------------------------------------------------
# Copying Python distribution to build directory
#-------------------------------------------------------------------------------
Write-Host "Copying Python distribution to build directory" -NoNewLine
Start-Process -FilePath "xcopy" `
			-ArgumentList "$PYTHON_DIR", "$BUILD_DIR", "/E", "/I", "/Y", "/H" `
			-Wait -WindowStyle Hidden
If ( Test-Path -Path "$BLD_PY_BIN" ) {
    Write-Result "Success" -ForegroundColor Green
} else {
    Write-Result "Failed" -ForegroundColor Red
    exit 1
}

#-------------------------------------------------------------------------------
# Removing Unneeded files from Python
#-------------------------------------------------------------------------------
$remove = "idlelib",
          "test",
          "tkinter",
          "turtledemo"
$remove | ForEach-Object {
    if ( Test-Path -Path "$BUILD_DIR\Lib\$_" ) {
        Write-Host "Removing $_`: " -NoNewline
        Remove-Item -Path "$BUILD_DIR\Lib\$_" -Recurse -Force
        if (Test-Path -Path "$BUILD_DIR\Lib\$_") {
            Write-Result "Failed" -ForegroundColor Red
            exit 1
        } else {
            Write-Result "Success" -ForegroundColor Green
        }
    }
}

#-------------------------------------------------------------------------------
# Restoring Original Global Script Preferences
#-------------------------------------------------------------------------------
if ( $CREATED_POWERSHELL_PROFILE_DIRECTORY ) {
    Write-Host "Removing PowerShell Profile Directory: " -NoNewline
    Remove-Item -Path "$(Split-Path "$profile" -Parent)" -Recurse -Force
    if ( !  (Test-Path -Path "$(Split-Path "$profile" -Parent)") ) {
        Write-Result "Success" -ForegroundColor Green
    } else {
        Write-Result "Failure" -ForegroundColor Red
        exit 1
    }
}

if ( Test-Path -Path "$profile" ) {
    Write-Host "Removing Temporary PowerShell Profile: " -NoNewline
    Remove-Item -Path "$profile" -Force
    if ( ! (Test-Path -Path "$profile") ) {
        Write-Result "Success" -ForegroundColor Green
    } else {
        Write-Result "Failed" -ForegroundColor Red
        exit 1
    }
}

if ( Test-Path -Path "$profile.salt_bak" ) {
    Write-Host "Restoring Original PowerShell Profile: " -NoNewline
    Move-Item -Path "$profile.salt_bak" -Destination "$profile"
    if ( Test-Path -Path "$profile" ) {
        Write-Result "Success" -ForegroundColor Green
    } else {
        Write-Result "Failed" -ForegroundColor Red
        exit 1
    }
}

#-------------------------------------------------------------------------------
# Finished
#-------------------------------------------------------------------------------
Write-Host $("-" * 80)
Write-Host "$SCRIPT_MSG Completed" -ForegroundColor Cyan
Write-Host "Environment Location: $BUILD_DIR"
Write-Host $("=" * 80)
