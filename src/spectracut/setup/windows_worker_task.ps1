# SPDX-License-Identifier: AGPL-3.0-or-later
# One-time admin script for a SpectraCut render host whose SSH account is not an Administrator.
# Run it once in an elevated PowerShell (Run as administrator). It registers the task
# `spectracut-worker` to run windows_worker.ps1 at startup as the SSH account (S4U: no
# password stored or passed) and lets that account start and end the task, so later
# `spectracut setup` runs manage the worker with schtasks.exe and no admin.
# Setup writes this file under the account's profile with the placeholders filled in.
$ErrorActionPreference = 'Stop'
$account = '__ACCOUNT__'
$sid = '__SID__'
$profileDir = '__HOME__'
$task = 'spectracut-worker'
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Run this script from an elevated PowerShell (Run as administrator).'
}
$script = [Security.SecurityElement]::Escape((Join-Path $profileDir 'spectracut\windows_worker.ps1'))
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>SpectraCut Flamenco worker</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$sid</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$script"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
$file = Join-Path $env:TEMP 'spectracut-worker-task.xml'
[IO.File]::WriteAllText($file, $xml, [Text.Encoding]::Unicode)
try {
  schtasks.exe /create /tn $task /xml $file /f | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "schtasks /create failed ($LASTEXITCODE)" }
} finally {
  Remove-Item $file -Force -ErrorAction SilentlyContinue
}
# Read + execute on the task lets the account run and end it (Task Scheduler COM, not CIM).
$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$registered = $svc.GetFolder('\').GetTask($task)
$sd = $registered.GetSecurityDescriptor(4)
$ace = "(A;;GRGX;;;$sid)"
if (-not $sd.Contains($ace)) { $registered.SetSecurityDescriptor($sd + $ace, 0) }
schtasks.exe /run /tn $task | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks /run failed ($LASTEXITCODE)" }
Write-Output "registered $task for $account; it starts at boot and spectracut setup can now manage it"
