<#
.SYNOPSIS
Parent script that runs all other scripts required to build Salt

.DESCRIPTION
This script builds Python, Installs Salt, and runs Windows-specific postprocessing logic. It depends on the following Scripts
and are called in this order:

- build_python.ps1
- install_salt.ps1
- build_pkg.ps1

.EXAMPLE
build.ps1

.EXAMPLE
build.ps1 -Version 3006 -PythonDir C:\Python310
#>

param(
    [Parameter(Mandatory=$false)]
    [Alias("v")]
    # The version of Salt to be built. If this is not passed, the script will
    # attempt to get it from the git describe command on the Salt source
    # repo
    [String] $Version,

    [Parameter(Mandatory=$false)]
    [Alias("c")]
    # Don't pretify the output of the Write-Result
    [Switch] $CICD,
	
	[Parameter(Mandatory=$false)]    
    # Directory of the Python installation to use
    [String] $PythonDir
)

#-------------------------------------------------------------------------------
# Script Preferences
#-------------------------------------------------------------------------------

$ProgressPreference = "SilentlyContinue"
$ErrorActionPreference = "Stop"

#-------------------------------------------------------------------------------
# Variables
#-------------------------------------------------------------------------------
$SCRIPT_DIR     = (Get-ChildItem "$($myInvocation.MyCommand.Definition)").DirectoryName
$PROJECT_DIR    = $(git rev-parse --show-toplevel)

if ( $Architecture -eq "amd64" ) {
  $Architecture = "x64"
}

#-------------------------------------------------------------------------------
# Verify Salt and Version
#-------------------------------------------------------------------------------

if ( [String]::IsNullOrEmpty($Version) ) {
    if ( ! (Test-Path -Path $PROJECT_DIR) ) {
        Write-Host "Missing Salt Source Directory: $PROJECT_DIR"
        exit 1
    }
    Push-Location $PROJECT_DIR
    $Version = $( git describe )
    $Version = $Version.Trim("v")
    Pop-Location
    if ( [String]::IsNullOrEmpty($Version) ) {
        Write-Host "Failed to get version from $PROJECT_DIR"
        exit 1
    }
}

#-------------------------------------------------------------------------------
# Start the Script
#-------------------------------------------------------------------------------

Write-Host $("#" * 80)
Write-Host "Build Salt Installer Packages" -ForegroundColor Cyan
Write-Host "- Salt Version:   $Version"
Write-Host $("v" * 80)

#-------------------------------------------------------------------------------
# Build Python
#-------------------------------------------------------------------------------

$KeywordArguments = @{}
if ( $Build ) {
	$KeywordArguments["Build"] = $false
}
if ( $CICD ) {
	$KeywordArguments["CICD"] = $true
}
if ( $PythonDir ) {
	$KeywordArguments["PythonDir"] = $PythonDir
}

& "$SCRIPT_DIR\build_python.ps1" @KeywordArguments
if ( ! $? ) {
	Write-Host "Failed to build Python"
	exit 1
}

#-------------------------------------------------------------------------------
# Install Salt
#-------------------------------------------------------------------------------

$KeywordArguments = @{}
if ( $CICD ) {
    $KeywordArguments["CICD"] = $true
}
if ( $SkipInstall ) {
    $KeywordArguments["SkipInstall"] = $true
}

$KeywordArguments["PKG"] = $true
& "$SCRIPT_DIR\install_salt.ps1" @KeywordArguments
if ( ! $? ) {
    Write-Host "Failed to install Salt"
    exit 1
}

#-------------------------------------------------------------------------------
# Prep Salt for Packaging
#-------------------------------------------------------------------------------

$KeywordArguments = @{}
if ( $CICD ) {
    $KeywordArguments["CICD"] = $true
}
$KeywordArguments["PKG"] = $true
& "$SCRIPT_DIR\prep_salt.ps1" @KeywordArguments
if ( ! $? ) {
    Write-Host "Failed to Prepare Salt for packaging"
    exit 1
}

#-------------------------------------------------------------------------------
# Build NSIS Package
#-------------------------------------------------------------------------------

$KeywordArguments = @{}
if ( ! [String]::IsNullOrEmpty($Version) ) {
    $KeywordArguments.Add("Version", $Version)
}
if ( $CICD ) {
    $KeywordArguments["CICD"] = $true
}

& "$SCRIPT_DIR\nsis\build_pkg.ps1" @KeywordArguments

if ( ! $? ) {
    Write-Host "Failed to build NSIS package"
    exit 1
}

#-------------------------------------------------------------------------------
# Script Complete
#-------------------------------------------------------------------------------

Write-Host $("^" * 80)
Write-Host "Build Salt $Architecture Completed" -ForegroundColor Cyan
Write-Host $("#" * 80)
