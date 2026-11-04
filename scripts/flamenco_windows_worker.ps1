# SpectraCut Flamenco worker: map the shared storage as F: and run the worker (issue #79/#50).
$ErrorActionPreference = 'Stop'
$home_ = Join-Path $env:USERPROFILE 'spectracut'
$pw = (Get-Content (Join-Path $home_ 'smb.pw') -Raw).Trim() | ConvertTo-SecureString -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential('MRDUBEY\flamenco', $pw)
$local = 'D:\SpectraCut\flamenco'   # set when this PC hosts the storage itself
if (-not (Test-Path S:\jobs)) {
  cmd /c "net use S: /delete /y >nul 2>&1"
  cmd /c "subst S: /d >nul 2>&1"
  if ((Test-Path $local) -and ($env:COMPUTERNAME -eq 'MRDUBEY')) { cmd /c "subst S: $local" }
  else { New-PSDrive -Name S -PSProvider FileSystem -Root '\\100.90.48.79\flamenco' -Credential $cred -Persist -Scope Global | Out-Null }
}
"S: mapped: " + (Test-Path S:\jobs\storage-check\cycles.blend)
# Let the worker find Blender itself: register this Blender for .blend files for this user (no admin needed).
$blender = Join-Path $home_ 'blender-4.5.15-windows-x64\blender.exe'
New-Item -Path 'HKCU:\Software\Classes\.blend' -Force | Out-Null
Set-ItemProperty -Path 'HKCU:\Software\Classes\.blend' -Name '(default)' -Value 'blendfile'
New-Item -Path 'HKCU:\Software\Classes\blendfile\shell\open\command' -Force | Out-Null
Set-ItemProperty -Path 'HKCU:\Software\Classes\blendfile\shell\open\command' -Name '(default)' -Value ('"' + $blender + '" "%1"')
# The worker resolves the Manager's 'blender' value via PATH when no desktop session is available.
$env:PATH = (Split-Path $blender) + ';' + $env:PATH
Set-Location $home_
& .\flamenco-worker.exe 2>&1 | ForEach-Object { "$_" }
