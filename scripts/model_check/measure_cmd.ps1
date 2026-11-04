# Measure any CLI: peak VRAM (nvidia-smi), peak RAM of the process, wall time; appends to out\results.jsonl.
# Usage: measure_cmd.ps1 -Job foley -Model "HunyuanVideo-Foley XL" -Settings "offload" -Tag foley_xl -Exe <path> -Args "a b c" [-Cwd dir]
param([string]$Job,[string]$Model,[string]$Settings,[string]$Tag,[string]$Exe,[string]$Args,[string]$Cwd="")
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$r=$env:SC_MODELS; $env:HF_HOME="$r\hf"; $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="$r\model_check"
if($Cwd){ Set-Location $Cwd }
$o="$r\out\$Tag.stdout.txt"; $e="$r\out\$Tag.stderr.txt"
$base=[int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits); $sw=[Diagnostics.Stopwatch]::StartNew()
$p=Start-Process $Exe -ArgumentList $Args -PassThru -NoNewWindow -RedirectStandardOutput $o -RedirectStandardError $e
$pv=$base; $pr=0
while(-not $p.HasExited){ $v=[int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits); if($v -gt $pv){$pv=$v}; $p.Refresh(); if($p.WorkingSet64/1MB -gt $pr){$pr=[int]($p.WorkingSet64/1MB)}; Start-Sleep -Milliseconds 300 }
$p.WaitForExit(); $sw.Stop()
$rec=@{job=$Job;model=$Model;settings=$Settings;host=$env:COMPUTERNAME;ok=($p.ExitCode -eq 0);exit=$p.ExitCode;peak_vram_mib=($pv-$base);peak_ram_mib=$pr;seconds=[math]::Round($sw.Elapsed.TotalSeconds,1);extra=@{tail=((Get-Content $o -Tail 3) -join " | ")}}
($rec | ConvertTo-Json -Compress -Depth 4) | Add-Content "$r\out\results.jsonl"
"RESULT " + ($rec | ConvertTo-Json -Compress -Depth 4)
