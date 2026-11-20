$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$pythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $pythonwCmd) { exit 1 }
$pythonw = $pythonwCmd.Source

$script = Get-ChildItem -Path $root -Directory |
    ForEach-Object { Join-Path $_.FullName "sjtu_chat.pyw" } |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1
if (-not $script) { exit 1 }
$workdir = Split-Path -Parent $script
$icon = Join-Path $workdir "icon.ico"
$lnk = Join-Path $root "交大致远一号.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $pythonw
$sc.Arguments = '"' + $script + '"'
$sc.WorkingDirectory = $workdir
if (Test-Path $icon) { $sc.IconLocation = $icon }
$sc.Description = "交大 致远一号 对话窗口"
$sc.Save()
Write-Output "LNK_OK"
exit 0
