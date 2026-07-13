$ErrorActionPreference = "Stop"

$repoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$launcher = Join-Path $repoDir "GUI_stim_and_cam_launcher.pyw"
$icon = Join-Path $repoDir "GUI_icon.ico"
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "GUI Stim and Cam.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $repoDir
$shortcut.IconLocation = $icon
$shortcut.Description = "Launch the stimulus and camera GUI."
$shortcut.Save()

Write-Host "Created $shortcutPath"
