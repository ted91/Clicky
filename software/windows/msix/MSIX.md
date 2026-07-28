# Packaging Clicky as a sideloadable MSIX (Windows)

This turns the plain PyInstaller build (`dist\Clicky\Clicky.exe` + its
supporting files) into an `.msix` — a proper Windows app package with a
Start Menu entry, an entry in "Installed apps" (clean uninstall, no
leftover files), and a real app identity — instead of a raw folder + a
`.bat` script.

This is **sideloading**, not a Microsoft Store listing: no developer
account, no Store review, no fee. The tradeoff is that Windows will only
install an MSIX signed by a certificate it trusts, so `build-msix.ps1`
below generates a free self-signed certificate and you (or anyone you
share the `.msix` with) have to explicitly trust it once per machine — a
different flavor of the same "unsigned build" warning as the SmartScreen
prompt on the plain `.exe`/DMG builds, not an extra restriction beyond it.

## Prerequisites (one-time, on the Windows machine)

1. Build the plain `.exe` first, same as always:
   ```
   .venv\Scripts\pyinstaller.exe clicky_windows.spec --noconfirm
   ```
2. Get `makeappx.exe` and `signtool.exe` on your PATH. Either:
   - Install the **MSIX Packaging Tool** from the Microsoft Store (free) —
     these ship inside it under
     `C:\Program Files (x86)\Windows Kits\10\bin\<version>\x64\`, or
   - Install the full **Windows SDK**
     (https://developer.microsoft.com/windows/downloads/windows-sdk/), or
   - Just run `build-msix.ps1` from a **"Developer PowerShell for VS"**
     prompt (Visual Studio Installer → Individual Components → make sure
     "Windows SDK" is checked) — that prompt already has these on PATH.

## Build

```
powershell -ExecutionPolicy Bypass -File msix\build-msix.ps1
```

This stages `dist\Clicky\*` + `msix\Assets\*` + `msix\AppxManifest.xml`
into `dist\_msix_staging\`, packs it with `makeappx.exe`, generates (or
reuses) a self-signed cert named `CN=Clicky Dev`, signs the package with
`signtool.exe`, and prints the exact install commands. Result:
`dist\Clicky.msix`, `dist\ClickySelfSigned.cer` (the trust certificate —
share this alongside the `.msix`), `dist\ClickySelfSigned.pfx` (the
private signing key — keep this one, don't share it).

## Installing (yours or anyone else's machine)

Trusting the cert needs Administrator once per machine:
```
Import-Certificate -FilePath ClickySelfSigned.cer -CertStoreLocation Cert:\LocalMachine\TrustedPeople
```
Then, no admin needed:
```
Add-AppxPackage -Path Clicky.msix
```
Clicky now shows up in the Start Menu and in Settings → Apps like any
normal install.

## Uninstalling

```
Remove-AppxPackage -Package Clicky.VoiceMemoPipeline
```
Same as the `.bat`/`.command` installers: this never touches
`%USERPROFILE%\.clicky-pipeline\` (recordings, settings, API keys) —
only the app files themselves. Notion state persists across
reinstalls the same way.

## Rebuilding after a code change

Bump the `Version` in `AppxManifest.xml` (`Add-AppxPackage` refuses to
install the same version number over an existing install — `Version="1.0.0.0"`
→ `"1.0.1.0"`, etc.), rerun the PyInstaller build, then rerun
`build-msix.ps1`. The signing cert is reused automatically (matched by its
Subject), so `Import-Certificate` only has to happen again on a machine
that hasn't installed a Clicky-signed package before.

## If you later want the actual Microsoft Store

This self-signed flow is a dead end for that path — Store submissions
need a Microsoft Partner Center developer account, a Store-issued app
identity (replaces the placeholder `Publisher`/`Name` in
`AppxManifest.xml`), and passing Store certification. That's a separate,
larger effort (account fee, packaging via Partner Center's own tooling,
review turnaround) — worth a fresh conversation if you want to go there,
since the manifest and signing setup here are specifically the
sideload-only shape.
