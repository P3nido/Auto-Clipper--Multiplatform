import sys
from pathlib import Path

# Permite "import hype_detector" etc. a partir da raiz do projeto (layout plano).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
