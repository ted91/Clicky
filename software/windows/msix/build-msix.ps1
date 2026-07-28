# Packages the already-built dist\Clicky\ folder (see clicky_windows.spec)
# into a signed, sideloadable Clicky.msix.
#
# MUST run on Windows -- makeappx.exe/signtool.exe come from the Windows
# SDK, same cross-compile limitation as PyInstaller itself (see
# clicky_windows.spec's own comment).
#
# Prerequisites (one-time):
#   1. Build the app first: .venv\Scripts\pyinstaller.exe clicky_windows.spec --noconfirm
#      (this script expects dist\Clicky\Clicky.exe to already exist)
#   2. Windows SDK signing tools on PATH -- easiest way: install the free
#      "Windows App Certification Kit" or full Windows SDK from
#      https://developer.microsoft.com/windows/downloads/windows-sdk/,
#      or just install "MSIX Packaging Tool" from the Microsoft Store,
#      which bundles makeappx.exe/signtool.exe under
#      C:\Program Files (x86)\Windows Kits\10\bin\<version>\x64\
#      Add that folder to PATH, or run this script from a "Developer
#      PowerShell for VS" prompt which already has it on PATH.
#
# Usage (from pipeline\, after step 1 above):
#   powershell -ExecutionPolicy Bypass -File msix\build-msix.ps1
#
# Produces: dist\Clicky.msix, plus (first run only) a self-signed cert
# installed to your CurrentUser\My store and exported alongside the
# package as ClickySelfSigned.cer -- import that .cer into
# "Local Computer\Trusted People" (see this script's final output, or
# INSTALL.md) on any machine you want to install the package on, since
# Windows refuses to install an MSIX signed by a cert it doesn't trust,
# even for your own sideloaded package on the very machine that made it.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $root "dist\Clicky"
$msixDir = $PSScriptRoot
$stagingDir = Join-Path $root "dist\_msix_staging"
$outMsix = Join-Path $root "dist\Clicky.msix"
$certSubject = "CN=Clicky Dev"
$certPath = Join-Path $root "dist\ClickySelfSigned.pfx"
$cerPath = Join-Path $root "dist\ClickySelfSigned.cer"
$certPassword = "clicky-local-signing"  # only protects a throwaway local dev cert, not a secret worth guarding

if (-not (Test-Path (Join-Path $distDir "Clicky.exe"))) {
    Write-Error "dist\Clicky\Clicky.exe not found -- run '.venv\Scripts\pyinstaller.exe clicky_windows.spec --noconfirm' first."
}

# --- Stage the package layout: PyInstaller output + Assets + manifest ---
if (Test-Path $stagingDir) { Remove-Item $stagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $stagingDir | Out-Null
Copy-Item "$distDir\*" $stagingDir -Recurse
Copy-Item (Join-Path $msixDir "AppxManifest.xml") $stagingDir
New-Item -ItemType Directory -Path (Join-Path $stagingDir "Assets") | Out-Null
Copy-Item (Join-Path $msixDir "Assets\*") (Join-Path $stagingDir "Assets") -Recurse

# --- makeappx: pack the staged folder into an .msix ---
$makeappx = Get-Command makeappx.exe -ErrorAction SilentlyContinue
if (-not $makeappx) {
    Write-Error "makeappx.exe not found on PATH -- see this script's header comment for where to get it (Windows SDK / MSIX Packaging Tool), or run this from a 'Developer PowerShell for VS' prompt."
}
if (Test-Path $outMsix) { Remove-Item $outMsix -Force }
& makeappx.exe pack /d $stagingDir /p $outMsix
if ($LASTEXITCODE -ne 0) { Write-Error "makeappx failed (exit $LASTEXITCODE)" }

# --- Self-signed cert: reuse if already generated, else create one ---
$existing = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $certSubject }
if ($existing) {
    $cert = $existing[0]
    Write-Host "Reusing existing signing cert: $($cert.Thumbprint)"
} else {
    Write-Host "Generating a new self-signed signing cert ($certSubject)..."
    $cert = New-SelfSignedCertificate -Type Custom -Subject $certSubject `
        -KeyUsage DigitalSignature -FriendlyName "Clicky MSIX signing (local, self-signed)" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
    $securePw = ConvertTo-SecureString -String $certPassword -Force -AsPlainText
    Export-PfxCertificate -Cert $cert -FilePath $certPath -Password $securePw | Out-Null
    Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
}

# --- signtool: sign the .msix with that cert ---
$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if (-not $signtool) {
    Write-Error "signtool.exe not found on PATH -- same source as makeappx.exe, see header comment."
}
& signtool.exe sign /fd SHA256 /a /s My /n $certSubject.Substring(3) $outMsix
if ($LASTEXITCODE -ne 0) { Write-Error "signtool failed (exit $LASTEXITCODE)" }

Remove-Item $stagingDir -Recurse -Force

Write-Host ""
Write-Host "Built and signed: $outMsix"
Write-Host ""
Write-Host "ONE-TIME per machine before install can succeed: trust the signing cert."
Write-Host "Run as Administrator (once, on each machine you install to -- including this one):"
Write-Host "  Import-Certificate -FilePath `"$cerPath`" -CertStoreLocation Cert:\LocalMachine\TrustedPeople"
Write-Host ""
Write-Host "Then install with:"
Write-Host "  Add-AppxPackage -Path `"$outMsix`""
Write-Host ""
Write-Host "Uninstall later with:"
Write-Host "  Remove-AppxPackage -Package Clicky.VoiceMemoPipeline"
