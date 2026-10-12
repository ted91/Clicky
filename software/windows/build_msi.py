"""Package dist\\Clicky into a per-user Windows Installer package (.msi), no extra tools needed
(uses Python 3.12's built-in msilib; msilib was removed in Python 3.13).

    pyinstaller clicky_windows.spec --noconfirm   # 1. -> dist\\Clicky\\Clicky.exe
    python build_msi.py --version 0.11.1          # 2. -> dist\\Clicky-<version>.msi  (--source/--out to override)

Same approach as the Live Subtitles installer:
  * PER-USER: installs to %LOCALAPPDATA%\\Programs\\Clicky, no admin rights and no trusted certificate needed
  * Start-menu shortcut "Clicky"
  * listed in Settings > Apps; newer versions upgrade older ones
  * user data in %USERPROFILE%\\.clicky-pipeline is never touched by install/upgrade/uninstall

Install:   double-click, or   msiexec /i Clicky-0.11.1.msi            (add /qn for silent)
Uninstall: Settings > Apps, or msiexec /x Clicky-0.11.1.msi
Log:       msiexec /i ... /l*v install.log
"""
import argparse
import sys
import uuid
import warnings
from pathlib import Path

warnings.simplefilter("ignore", DeprecationWarning)
import msilib  # noqa: E402
import msilib.schema  # noqa: E402,F401
import msilib.sequence  # noqa: E402,F401
from msilib import Directory, Feature, add_data  # noqa: E402

ROOT = Path(__file__).parent
NAME = "Clicky"
MANUFACTURER = "Clicky"
UPGRADE_CODE = "{B6D2E4C8-5A1F-4E37-8D90-3C7A9E21F5B4}"      # never change: identifies the product family
NS = uuid.UUID("9a4b7c2e-1d3f-4a68-b5e0-7c2d9f1a3b64")        # for stable component GUIDs
REG_KEY = r"Software\Clicky\Components"


def guid_for(text):
    return "{" + str(uuid.uuid5(NS, text)).upper() + "}"


def add_ui(db):
    """A small install wizard: Welcome -> progress bar with the current step -> Finish (with a
    "Start Clicky now" checkbox). Only shown for interactive installs; /qn and /qb skip it."""
    VIS, MODAL, MINI = 1, 2, 4
    TITLE, BODY = r"{\TitleFont}", ""

    add_data(db, "TextStyle", [("DefaultFont", "Tahoma", 8, None, 0), ("TitleFont", "Tahoma", 14, None, 1)])
    add_data(db, "CheckBox", [("LAUNCHAPP", "1")])
    add_data(db, "ActionText", [
        ("InstallInitialize", "Starting setup...", None),
        ("InstallFiles", "Copying Clicky's files...", None),
        ("RemoveFiles", "Removing Clicky's files...", None),
        ("CreateShortcuts", "Creating the Start menu shortcut...", None),
        ("RemoveShortcuts", "Removing the Start menu shortcut...", None),
        ("WriteRegistryValues", "Saving settings...", None),
        ("RemoveRegistryValues", "Cleaning up settings...", None),
        ("RegisterProduct", "Registering Clicky...", None),
        ("PublishProduct", "Registering Clicky...", None),
        ("InstallFinalize", "Finishing up...", None),
    ])

    dialogs = [  # Dialog, HCenter, VCenter, W, H, attrs, title, first, default, cancel
        ("WelcomeDlg", 50, 50, 370, 270, VIS | MODAL | MINI, "Clicky Setup", "InstallBtn", "InstallBtn", "CancelBtn"),
        ("ProgressDlg", 50, 50, 370, 270, VIS | MINI, "Clicky Setup", "CancelBtn", "CancelBtn", "CancelBtn"),
        ("ExitDialog", 50, 50, 370, 270, VIS | MODAL | MINI, "Clicky Setup", "FinishBtn", "FinishBtn", "FinishBtn"),
        ("UserExit", 50, 50, 370, 270, VIS | MODAL | MINI, "Clicky Setup", "FinishBtn", "FinishBtn", "FinishBtn"),
        ("FatalError", 50, 50, 370, 270, VIS | MODAL | MINI, "Clicky Setup", "FinishBtn", "FinishBtn", "FinishBtn"),
    ]
    add_data(db, "Dialog", dialogs)

    def text(d, name, x, y, w, h, t):
        return (d, name, "Text", x, y, w, h, 3, None, t, None, None)

    def button(d, name, x, y, t, nxt):
        return (d, name, "PushButton", x, y, 56, 17, 3, None, t, nxt, None)

    def line(d):
        return (d, "BottomLine", "Line", 0, 234, 370, 0, 1, None, None, None, None)

    controls = [
        # Welcome
        text("WelcomeDlg", "Title", 20, 15, 330, 30, TITLE + "Welcome to Clicky"),
        text("WelcomeDlg", "Body", 20, 60, 330, 150,
             "Setup will install Clicky for your Windows user account. No administrator rights are needed.\r\n\r\n"
             "Install folder:\r\n[INSTALLDIR]\r\n\r\n"
             "When setup finishes, Clicky runs in the system tray (the ^ arrow next to the clock) and opens its "
             "dashboard in your browser. Your recordings and settings are never touched by setup."),
        line("WelcomeDlg"),
        button("WelcomeDlg", "InstallBtn", 236, 243, "&Install", "CancelBtn"),
        button("WelcomeDlg", "CancelBtn", 304, 243, "Cancel", "InstallBtn"),
        # Progress
        text("ProgressDlg", "Title", 20, 15, 330, 30, TITLE + "Installing Clicky"),
        text("ProgressDlg", "Body", 20, 60, 330, 40,
             "Please wait while setup copies Clicky's files. This can take a minute or two."),
        text("ProgressDlg", "ActionText", 20, 115, 330, 17, "Starting setup..."),
        ("ProgressDlg", "ProgressBar", "ProgressBar", 20, 140, 330, 17, 65539, None, "Progress done", None, None),
        line("ProgressDlg"),
        button("ProgressDlg", "CancelBtn", 304, 243, "Cancel", None),
        # Finish
        text("ExitDialog", "Title", 20, 15, 330, 30, TITLE + "Setup finished"),
        text("ExitDialog", "InstalledText", 20, 60, 330, 110,
             "Clicky is installed.\r\n\r\nLook for the Clicky icon in the system tray (click the ^ arrow next to the "
             "clock if you don't see it). Click it to open the dashboard at http://127.0.0.1:8000, or right-click it to quit."),
        text("ExitDialog", "RemovedText", 20, 60, 330, 110,
             "Clicky has been removed.\r\n\r\nYour recordings and settings in your user profile folder "
             "(.clicky-pipeline) were kept."),
        ("ExitDialog", "LaunchCheck", "CheckBox", 20, 190, 330, 18, 3, "LAUNCHAPP",
         "Start Clicky now and open the dashboard", None, None),
        line("ExitDialog"),
        button("ExitDialog", "FinishBtn", 304, 243, "&Finish", None),
        # Cancelled / failed
        text("UserExit", "Title", 20, 15, 330, 30, TITLE + "Setup was cancelled"),
        text("UserExit", "Body", 20, 60, 330, 60, "Clicky was not installed. You can run the installer again at any time."),
        line("UserExit"),
        button("UserExit", "FinishBtn", 304, 243, "&Finish", None),
        text("FatalError", "Title", 20, 15, 330, 30, TITLE + "Setup did not finish"),
        text("FatalError", "Body", 20, 60, 330, 60,
             "Setup ended before Clicky finished installing. Nothing was changed on your account that needs cleaning up; "
             "you can run the installer again."),
        line("FatalError"),
        button("FatalError", "FinishBtn", 304, 243, "&Finish", None),
    ]
    add_data(db, "Control", controls)

    add_data(db, "ControlEvent", [
        ("WelcomeDlg", "InstallBtn", "EndDialog", "Return", "1", 1),
        ("WelcomeDlg", "CancelBtn", "EndDialog", "Exit", "1", 1),
        ("ProgressDlg", "CancelBtn", "EndDialog", "Exit", "1", 1),
        ("ExitDialog", "FinishBtn", "DoAction", "LaunchApp", 'LAUNCHAPP="1" AND NOT REMOVE', 1),
        ("ExitDialog", "FinishBtn", "EndDialog", "Return", "1", 2),
        ("UserExit", "FinishBtn", "EndDialog", "Exit", "1", 1),
        ("FatalError", "FinishBtn", "EndDialog", "Exit", "1", 1),
    ])
    add_data(db, "ControlCondition", [
        ("ExitDialog", "InstalledText", "Hide", 'REMOVE="ALL"'),
        ("ExitDialog", "LaunchCheck", "Hide", 'REMOVE="ALL"'),
        ("ExitDialog", "RemovedText", "Hide", "NOT REMOVE"),
    ])
    add_data(db, "EventMapping", [
        ("ProgressDlg", "ActionText", "ActionText", "Text"),
        ("ProgressDlg", "ProgressBar", "SetProgress", "Progress"),
    ])
    # ExitDialog / FatalError / UserExit are already in the standard sequence (-1/-3/-2); add the two the wizard needs.
    add_data(db, "InstallUISequence", [("WelcomeDlg", "NOT Installed", 1230), ("ProgressDlg", None, 1280)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="0.11.1")
    ap.add_argument("--source", default=str(ROOT / "dist" / "Clicky"))
    ap.add_argument("--out")
    a = ap.parse_args()

    src = Path(a.source)
    if not (src / "Clicky.exe").exists():
        sys.exit(f"{src}\\Clicky.exe not found - build the PyInstaller output first")
    out = Path(a.out) if a.out else ROOT / "dist" / f"Clicky-{a.version}.msi"
    product_code = msilib.gen_uuid()

    db = msilib.init_database(str(out), msilib.schema, NAME, product_code, a.version, MANUFACTURER)
    msilib.add_tables(db, msilib.sequence)
    # per-user package: summary "word count" bit 8 = elevated privileges not required
    si = db.GetSummaryInformation(20)
    si.SetProperty(msilib.PID_WORDCOUNT, 2 | 8)
    si.Persist()

    cab = msilib.CAB("Clicky")
    Feature(db, "Main", "Clicky", "The Clicky app", 1, directory="INSTALLDIR")

    add_data(db, "Directory", [
        ("TARGETDIR", None, "SourceDir"),
        ("LocalAppDataFolder", "TARGETDIR", "."),
        ("ProgramMenuFolder", "TARGETDIR", "."),       # per-user Start menu because ALLUSERS is not set
        ("ProgramsLA", "LocalAppDataFolder", "Programs"),
        ("INSTALLDIR", "ProgramsLA", "Clicky"),
    ])
    msilib._directories.add("INSTALLDIR")
    root = object.__new__(Directory)       # Directory() would write a parent-less row; we wrote our own
    root.__dict__.update(db=db, cab=cab, basedir=None, physical=str(src), logical="INSTALLDIR", component=None,
                         short_names=set(), ids=set(), keyfiles={}, componentflags=0, absolute=str(src))

    components = []
    exe_key = [None]

    def new_component(d, rel):
        cid = "C_" + d.logical
        d.component = cid
        add_data(db, "Component", [(cid, guid_for("dir:" + rel), d.logical, 4 | 256, None, "R_" + cid)])
        add_data(db, "FeatureComponents", [("Main", cid)])
        components.append(cid)

    def walk(d, folder, rel):
        files = sorted(p for p in folder.iterdir() if p.is_file())
        for p in files:
            if d.component is None:
                new_component(d, rel or ".")
            key = d.add_file(p.name)
            if rel == "" and p.name == "Clicky.exe":
                exe_key[0] = key
        for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
            short = d.make_short(sub.name)
            default = short if short == sub.name else f"{short}|{sub.name}"
            child = Directory(db, cab, d, sub.name, "D_" + (rel + "_" + sub.name).strip("_"), default)
            walk(child, sub, (rel + "/" + sub.name).strip("/"))

    print("collecting files ...")
    walk(root, src, "")
    n_files = len(cab.files)
    print(f"{n_files} files; compressing (a few minutes) ...")
    cab.commit(db)

    # Start-menu shortcut: own component with an HKCU key path so per-user installs self-heal
    cid = "StartMenuShortcut"
    add_data(db, "Component", [(cid, guid_for(cid), "ProgramMenuFolder", 4 | 256, None, "R_" + cid)])
    add_data(db, "FeatureComponents", [("Main", cid)])
    components.append(cid)
    add_data(db, "Shortcut", [(cid + "_lnk", "ProgramMenuFolder", "Clicky.lnk", cid, f"[#{exe_key[0]}]", None,
                               "Clicky voice-memo pipeline", None, None, None, None, "INSTALLDIR")])

    # Start Clicky from the finish page's checkbox. 18 = run an installed exe; +64 ignore exit code, +128 no wait.
    add_data(db, "CustomAction", [("LaunchApp", 18 + 64 + 128, exe_key[0], "")])

    add_data(db, "Registry", [("R_" + c, 1, REG_KEY, c, "1", c) for c in components])   # root 1 = HKCU
    add_data(db, "Registry", [("InstallDirReg", 1, r"Software\Clicky", "InstallDir", "[INSTALLDIR]", "StartMenuShortcut")])
    add_data(db, "RegLocator", [("INSTALLDIR_REG", 1, r"Software\Clicky", "InstallDir", 2)])
    add_data(db, "AppSearch", [("INSTALLDIR", "INSTALLDIR_REG")])

    add_data(db, "Property", [("UpgradeCode", UPGRADE_CODE),
                              ("ARPCOMMENTS", "Clicky voice-memo pipeline"),
                              ("ARPNOMODIFY", "1"),
                              ("DefaultUIFont", "DefaultFont"),
                              ("LAUNCHAPP", "1")])
    add_ui(db)
    add_data(db, "Upgrade", [(UPGRADE_CODE, "0.0.1", a.version, None, 256, None, "PREVIOUSVERSIONSINSTALLED")])
    have = {r[0] for r in msilib.sequence.InstallExecuteSequence}
    if "RemoveExistingProducts" not in have:
        add_data(db, "InstallExecuteSequence", [("RemoveExistingProducts", None, 6600)])
    db.Commit()
    size = out.stat().st_size / 1e6
    print(f"\nBuilt {out}  ({size:.0f} MB, {n_files} files, version {a.version})")


if __name__ == "__main__":
    main()
