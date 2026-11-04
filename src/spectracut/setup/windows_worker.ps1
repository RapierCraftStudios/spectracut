# SPDX-License-Identifier: AGPL-3.0-or-later
# SpectraCut Flamenco worker, run by the per-user logon task `spectracut-worker` (no admin needed).
# Maps the shared storage on -Drive (subst when this PC holds the storage itself), puts the
# profile's Blender on PATH and runs the worker. Installed to %USERPROFILE%\spectracut by setup.
param(
  [Parameter(Mandatory)][string]$Manager,
  [string]$Drive = 'F:',
  [string]$Share = 'flamenco',
  [string]$Server = '',
  [string]$User = '',
  [string]$LocalPath = ''
)
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:USERPROFILE 'spectracut'
$letter = $Drive.TrimEnd(':')
cmd /c "net use $Drive /delete /y >nul 2>&1"
cmd /c "subst $Drive /d >nul 2>&1"
if ($LocalPath) {
  New-Item -ItemType Directory -Force $LocalPath | Out-Null
  subst $Drive ($LocalPath -replace '/', '\')
} else {
  $pw = (Get-Content (Join-Path $root 'smb.pw') -Raw).Trim() | ConvertTo-SecureString -AsPlainText -Force
  $cred = New-Object System.Management.Automation.PSCredential($User, $pw)
  New-PSDrive -Name $letter -PSProvider FileSystem -Root "\\$Server\$Share" -Credential $cred -Persist -Scope Global | Out-Null
}
# The Manager's Windows `blender` variable is the special value `blender`: resolve it via PATH.
$blender = Get-ChildItem $root -Directory -Filter 'blender-*-windows-x64' | Sort-Object Name | Select-Object -Last 1
$env:PATH = $blender.FullName + ';' + $env:PATH
Set-Location $root
& .\flamenco-worker.exe -manager $Manager *>> (Join-Path $root 'worker.log')
