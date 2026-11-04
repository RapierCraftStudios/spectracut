# Omni listening critic via llama.cpp mtmd CLI. Samples peak VRAM (nvidia-smi) and peak RAM (process WS), appends to out\results.jsonl.
# Usage: omni_llama.ps1 -Gguf <name under gguf\> -Mmproj <file> -Audio <wav> [-NCpuMoe 0] [-Ngl 99] [-Tag label]
param([string]$Gguf,[string]$Mmproj,[string]$Audio,[int]$NCpuMoe=0,[string]$Cuda="13.4",[int]$Ngl=99,[string]$Tag="omni",
      [string]$Prompt="You are a sound editor. Listen to this clip and describe in 2 sentences what you hear, then rate its technical quality 1-5 and list any defects (clipping, noise, glitches, silence).")
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$r=$env:SC_MODELS; $cli="$r\llama.cpp-$Cuda\llama-mtmd-cli.exe"
$m=(Get-ChildItem "$r\gguf" -Recurse -Filter $Gguf | select -First 1).FullName; $p=(Get-ChildItem "$r\gguf" -Recurse -Filter $Mmproj | select -First 1).FullName
$a=@("-m",$m,"--mmproj",$p,"--audio",$Audio,"-p",$Prompt,"-ngl","$Ngl","-c","8192","--temp","0.2","-n","200")
if($NCpuMoe -gt 0){ $a += @("--n-cpu-moe","$NCpuMoe") }
$base=[int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
$sw=[Diagnostics.Stopwatch]::StartNew()
$o="$r\out\$Tag.stdout.txt"; $e="$r\out\$Tag.stderr.txt"
$proc=Start-Process $cli -ArgumentList ($a|%{ if($_ -match "\s"){"`"$_`""}else{$_} }) -PassThru -NoNewWindow -RedirectStandardOutput $o -RedirectStandardError $e
$peakV=$base; $peakR=0
while(-not $proc.HasExited){ $v=[int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits); if($v -gt $peakV){$peakV=$v}
  $proc.Refresh(); if($proc.WorkingSet64/1MB -gt $peakR){$peakR=[int]($proc.WorkingSet64/1MB)}; Start-Sleep -Milliseconds 300 }
$proc.WaitForExit(); $sw.Stop()
$rec=@{job="omni";model=$Tag;settings="ngl=$Ngl n_cpu_moe=$NCpuMoe audio=$(Split-Path $Audio -Leaf)";host=$env:COMPUTERNAME;ok=($proc.ExitCode -eq 0);
  peak_vram_mib=($peakV-$base);peak_ram_mib=$peakR;seconds=[math]::Round($sw.Elapsed.TotalSeconds,1);extra=@{answer=(([string](Get-Content $o)) -replace "\s+"," ").Trim()}}
($rec | ConvertTo-Json -Compress -Depth 4) | Add-Content "$r\out\results.jsonl"
"RESULT " + ($rec | ConvertTo-Json -Compress -Depth 4)
