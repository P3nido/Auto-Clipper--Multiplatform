# ADR-009: Executável para Windows (PyInstaller)

**Status:** Aceito · **Data:** 2026-11-17

## Contexto
O usuário quer abrir o Auto-Clipper com dois cliques, sem instalar Python, sem `pip` e sem terminal, numa máquina com i5-9400F + RTX 5060. O app depende de programas externos e de bibliotecas nativas:
- **FFmpeg/FFprobe:** build completo do Gyan com NVENC, Quick Sync e libass, ~455 MB.
- **Streamlink:** hoje roda como executável `streamlink.exe` em subprocess (ADR-004).
- **faster-whisper:** inclui ctranslate2, onnxruntime e o modelo VAD dentro do pacote.
- **Bibliotecas CUDA via pip:** cuBLAS/cuDNN, ~2 GB.
- **PySide6 e curl_cffi.**

## Decisão
1. **PyInstaller em modo pasta** (`dist/Auto-Clipper/`), não arquivo único. O modo arquivo único descompactaria ~3 GB a cada abertura.
2. **Dois executáveis na mesma pasta**, que compartilham `_internal/`:
   - `Auto-Clipper.exe`: janela, sem console, roda a interface (`gui.main`).
   - `auto-clipper-cli.exe`: console. Roda a CLI (`main.main`, incluindo `--diagnostico`) e o modo interno `--run-streamlink <args>`, que executa o `streamlink_cli` embutido. O `recorder` chama esse executável no lugar do `streamlink.exe` quando o app está empacotado.
3. **Novo módulo `runtime.py`:** concentra tudo que muda entre "rodando do código" e "empacotado".
   - `is_frozen()`.
   - `app_dir()`: pasta do .exe ou raiz do projeto.
   - `data_dir()`: `%LOCALAPPDATA%\Auto-Clipper` empacotado, raiz do projeto em desenvolvimento.
   - `default_sessions_dir()`: `%USERPROFILE%\Videos\Auto-Clipper` empacotado, `sessions` em desenvolvimento.
   - `bundled_tool(name)`: procura `ffmpeg/ffmpeg.exe` dentro do pacote.
   - `streamlink_command()`: `[streamlink.exe]` em desenvolvimento, `[auto-clipper-cli.exe, "--run-streamlink"]` empacotado.
   - `subprocess_flags()`: `CREATE_NO_WINDOW` no Windows, para nenhuma janela preta piscar quando o app chama FFmpeg, nvidia-smi ou PowerShell.
   - `nvidia_dll_dirs()`: as pastas `nvidia/*/bin` também dentro do pacote.
4. **Arquivos do usuário fora da pasta do programa:** `settings.json` e `app.log` em `data_dir()`, e as gravações em `Vídeos\Auto-Clipper`. Assim, atualizar o app trocando a pasta não apaga nada.
5. **Build reprodutível:** `build_exe.py` (Python, roda em qualquer terminal) faz o `pyinstaller auto_clipper.spec`, copia o FFmpeg e o FFprobe do PATH ou do winget para `dist/Auto-Clipper/ffmpeg/` e, no fim, roda um teste rápido do pacote (`auto-clipper-cli.exe --diagnostico`).
6. **Modelos do Whisper não vão no pacote.** São baixados no primeiro uso (~1,6 GB no `large-v3-turbo`) para o cache do Hugging Face. A interface avisa "Preparando as legendas pela primeira vez (download do modelo)".

## Consequências
- A pasta final tem ~3 GB e o zip ~1,5 GB. É o custo de levar NVENC + CUDA prontos.
- O Chrome continua necessário para gravar na Kick (Streamlink + Cloudflare) e não vai no pacote.
- O FFmpeg do Gyan é GPL: vai junto apenas para uso pessoal, e o pacote leva o aviso de licença.
- Qualquer subprocess novo no código precisa usar `runtime.subprocess_flags()`.
