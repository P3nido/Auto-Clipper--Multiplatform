# ADR-008: Várias lives em paralelo e interface gráfica

**Status:** Aceito · **Data:** 2026-11-17

## Contexto
O próximo passo é um executável com interface visual: o usuário cola o link de uma live e o app cuida do resto. Ele também quer acompanhar **mais de uma live ao mesmo tempo**. O código atual foi pensado para uma sessão por vez: `main.py` orquestra uma live, e `setup_logging` tem **um** arquivo de log global.

Custos medidos por live: gravação 1080p60 ~0,83 MB/s (~3 GB por hora, ~7 Mbps de rede), CPU baixa (o streamlink só copia bytes), chat desprezível. A parte pesada é o pós-processamento (Whisper + encode).

## Decisão
1. **Motor separado da interface:** módulo novo `engine.py` com um `JobManager`. A CLI (`main.py`) e a interface (`gui.py`) são só clientes dele.
   - Cada live é um **job** com thread própria: monitora → grava (Recorder + ChatListener, como hoje) → entrega a sessão para a fila → volta a monitorar, ou termina se `once`.
   - **Uma única fila de pós-processamento** (`POSTPROCESS_WORKERS = 1`) com o modelo Whisper carregado **uma vez** e reutilizado. Várias lives gravam em paralelo, mas os clipes saem um de cada vez, sem disputar a GPU.
   - Limite configurável: `MAX_PARALLEL_LIVES = 4`. O teto real é a rede e o disco.
   - O estado de cada job sai num `JobStatus`, um snapshot imutável, entregue por um callback `on_update`. A interface só observa e nunca mexe no motor diretamente.
2. **Log por live:** cada `run.log` recebe só as linhas daquela live. As threads de um job usam o prefixo `job-<id>`, e o handler de arquivo de cada sessão tem um filtro por esse prefixo. O console continua recebendo tudo.
3. **Interface em PySide6 (Qt):** ela trata bem várias atualizações vindas de threads (signals/slots), tem visual moderno e funciona com o PyInstaller.
   - MVP: um campo para colar o link e um botão Adicionar.
   - Um cartão por live, com plataforma, canal, estado em português, tempo gravado, mensagens de chat, picos e progresso dos clipes, mais os botões Parar e Abrir clipes.
   - Uma tela simples de ajustes e uma checagem amigável no primeiro uso (FFmpeg e GPU).
4. **Executável** (fase seguinte) com PyInstaller, numa pasta só, levando junto o FFmpeg.

## Consequências
- O `main.py` deixa de ter a lógica de orquestração (ela vai para o `engine.py`). Os testes de `main` passam a testar o `engine`.
- A interface pode ser construída em paralelo ao motor, contra o contrato do `engine.py`, usando um gerenciador falso para pré-visualização.
- Mais de uma live por vez multiplica disco e rede. A interface mostra o espaço livre e avisa quando estiver acabando.
