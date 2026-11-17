# ADR-007: Aceleração por GPU com detecção automática

**Status:** Aceito · **Data:** 2026-11-17

## Contexto
A máquina-alvo é um **i5-9400F** (6 núcleos/6 threads, **sem vídeo integrado**) com uma **RTX 5060** (Blackwell, 8 GB). A máquina de desenvolvimento é um i7-11390H com Intel Iris Xe e NVIDIA MX450 (2 GB, **sem NVENC**). O programa precisa usar a GPU ao máximo onde ela existir, sem quebrar onde não existir.

Medição na máquina de desenvolvimento (render vertical de 30 s, mesmo filtro):

| Pipeline | Tempo |
|---|---|
| CPU (libx264) | 29,4 s |
| encode Intel Quick Sync | 12,9 s |
| decode D3D11VA + encode Quick Sync | **9,3 s** |
| NVENC na MX450 | ❌ "No capable devices found" |

Pesquisa sobre Blackwell (RTX série 50) + CTranslate2, com base em relatos de usuários e não em documentação oficial: exige CUDA 12.8 ou mais novo, e o tipo automático `int8_float16` falha com `CUBLAS_STATUS_NOT_SUPPORTED`. A solução relatada é fixar `float16`.

## Decisão
Um módulo novo, `hardware.py`, detecta o hardware **uma vez** e devolve um `HardwareProfile`, usado pelo `clipper` e pelo `transcriber`:

| Peça | Ordem de preferência (`"auto"`) | Como valida |
|---|---|---|
| encode de vídeo | NVENC → Quick Sync → AMF → CPU (libx264) | encode real de 1 s com o FFmpeg |
| decode de vídeo | CUDA (com NVENC), D3D11VA (com Quick Sync/AMF), nenhum (CPU) | idem |
| Whisper | CUDA `float16` → CPU `int8` | `ctranslate2.get_cuda_device_count()`. O teste real acontece ao carregar o modelo, e qualquer falha volta para a CPU |
| modelo Whisper | `"auto"`: `large-v3-turbo` em CUDA, `small` em CPU | — |

- Tudo pode ser forçado no `config.py`: `VIDEO_ENCODER`, `HW_DECODE`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE` e `WHISPER_MODEL`.
- **Fallback em tempo de execução:** se um render na GPU falhar, o mesmo clipe é refeito em CPU e o motivo vai para o log. O programa nunca deixa de gerar o clipe por causa da GPU.
- **Bibliotecas CUDA do Whisper** via pip (`requirements-gpu.txt`: `nvidia-cublas-cu12>=12.8` e `nvidia-cudnn-cu12` 9). No Windows, as pastas `site-packages/nvidia/*/bin` entram com `os.add_dll_directory` antes de importar o ctranslate2.
- `python main.py --diagnostico` imprime o perfil detectado e um mini benchmark, para validar a RTX 5060 sem rodar uma live.

## Consequências
- Na RTX 5060, o vídeo vai para NVDEC/NVENC e as legendas para CUDA, e a CPU fica livre para as gravações. O encode de vídeo na GPU deve levar o clipe para poucos segundos, mas isso só será medido na própria máquina.
- O pacote fica maior (~1 GB a mais com as bibliotecas CUDA). As bibliotecas são opcionais: sem elas, o Whisper roda na CPU.
- O caminho NVENC/Blackwell **não pode ser testado na máquina de desenvolvimento**. É validado pelo `--diagnostico` na máquina-alvo.
