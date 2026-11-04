"""Download specific files: hf_get.py <repo> <file> [<file>...]  -> prints local paths (copies, no symlinks)."""
import sys
from common import ROOT
from huggingface_hub import hf_hub_download
for f in sys.argv[2:]:
    print(hf_hub_download(sys.argv[1], f, local_dir=f"{ROOT}/gguf/{sys.argv[1].split('/')[-1]}"))
