# SPDX-License-Identifier: AGPL-3.0-or-later
# Usage: setup_env.ps1 -Name tts -Packages "kokoro,soundfile" [-Python 3.11] [-Torch cu128] [-NoTorch]
# Creates $SC_MODELS\venvs\<Name> with CUDA torch + packages via uv. Per-user, no admin needed.
# Windows quirk (non-admin user on mrdubey-1): uv's "minor version" junction is untraversable
# (os error 448), so we drop the junction and point uv at the full-version interpreter dir.
param([string]$Name,[string]$Packages="",[string]$Python="3.11",[string]$Torch="cu128",[switch]$NoTorch)
$ErrorActionPreference="Continue"
if(-not $env:SC_MODELS){ $env:SC_MODELS = Split-Path $PSScriptRoot -Parent }
$root=$env:SC_MODELS; $v="$root\venvs\$Name"
$env:UV_PYTHON_INSTALL_DIR="$root\python"; $env:UV_CACHE_DIR="$root\uv-cache"; $env:HF_HOME="$root\hf"
$env:Path="$env:USERPROFILE\.local\bin;$env:Path"
uv python install $Python --no-bin 2>&1 | Out-Null
Get-ChildItem "$root\python" -Directory -ErrorAction SilentlyContinue | ? { $_.LinkType } | % { cmd /c rmdir "`"$($_.FullName)`"" }
$py=(Get-ChildItem "$root\python\cpython-$Python.*\python.exe" | Select -First 1).FullName
uv venv $v --python $py 2>&1 | Out-Null
$vp="$v\Scripts\python.exe"
if(-not $NoTorch){ uv pip install --python $vp torch torchaudio torchvision --index-url "https://download.pytorch.org/whl/$Torch" 2>&1 | Select -Last 3 }
if($Packages){ uv pip install --python $vp ($Packages -split '[ ,]+') 2>&1 | Select -Last 5 }
& $vp -c "import sys;print(sys.version)"
if(-not $NoTorch){ & $vp -c "import torch;print(torch.__version__,torch.cuda.is_available(),torch.cuda.get_device_name(0))" }
