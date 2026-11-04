# Download llama.cpp prebuilt Windows CUDA 13.4 binaries (+cudart) into $SC_MODELS\llama.cpp, then fetch GGUFs.
# Usage: setup_llamacpp.ps1 [-Tag b12110]
param([string]$Tag="b12110",[string]$Cuda="13.4")  # legion driver 581.80 = CUDA 13.0 -> use -Cuda 12.4 there
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$d="$env:SC_MODELS\llama.cpp-$Cuda"; New-Item -ItemType Directory -Force $d | Out-Null
foreach($n in "llama-$Tag-bin-win-cuda-$Cuda-x64.zip","cudart-llama-bin-win-cuda-$Cuda-x64.zip"){
  $f="$d\$n"; if(-not (Test-Path "$d\llama-server.exe") -or $n -like "cudart*"){ Invoke-WebRequest "https://github.com/ggml-org/llama.cpp/releases/download/$Tag/$n" -OutFile $f -UseBasicParsing; Expand-Archive $f $d -Force; Remove-Item $f }
}
Get-ChildItem $d -Filter "llama-*.exe" | select -First 8 Name
