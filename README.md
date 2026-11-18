# Auto-Clipper Multiplataforma

Monitora um canal da **Twitch**, **YouTube** ou **Kick** só pela URL, grava a live automaticamente, encontra os momentos de maior engajamento no chat, recorta clipes de 30 s **na vertical (9:16), prontos para Reels, Shorts e TikTok**, e queima legendas geradas localmente pelo Whisper. É **100% gratuito e local**: não precisa de conta, token nem chave de API.

> ✅ **Status:** v1 funcional, com as 5 etapas implementadas e validadas em lives reais (Twitch, Kick e YouTube) e 165 testes. Próximos passos de desempenho em [ARCHITECTURE.md §10](ARCHITECTURE.md#10-riscos-conhecidos).

## Pipeline

1. **Monitorar e gravar:** a cada 30 s verifica se a live está no ar e grava em `best` com o Streamlink.
2. **Capturar o chat:** em paralelo e de forma anônima (IRC da Twitch, histórico do chat da Kick e API interna do chat do YouTube).
3. **Detectar picos:** blocos de 10 s, média móvel, limiar de `SPIKE_MULTIPLIER`× e cooldown.
4. **Recortar:** 15 s antes e 15 s depois de cada pico, com o FFmpeg.
5. **Legendar:** Whisper local gera o `.srt`, e o FFmpeg queima a legenda no vídeo final.

## Instalação do zero (Windows)

Testado clonando o repositório numa pasta limpa: a instalação leva ~3 min e ocupa ~370 MB (sem PyTorch).

**1. Programas do sistema** (uma vez por máquina, no PowerShell):

```powershell
winget install Python.Python.3.13
winget install Git.Git
winget install Gyan.FFmpeg
```

- **Python 3.10 ou mais novo.** Se instalar pelo site python.org, marque **"Add python.exe to PATH"**.
- **FFmpeg + FFprobe:** usados no recorte e nas legendas. Depois do winget, **feche e reabra o terminal** para o `ffmpeg` entrar no PATH. Se esquecer, o programa encontra sozinho o atalho do winget.
- **Google Chrome ou Chromium:** só para **Kick**, por causa do desafio do Cloudflare.

**2. Projeto e dependências Python:**

```powershell
git clone https://github.com/P3nido/Auto-Clipper--Multiplatform.git auto-clipper
cd auto-clipper
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Os comandos chamam `.venv\Scripts\python` direto, sem `activate`. Assim não esbarram na política de execução de scripts do PowerShell.

**3. Conferir a instalação** (opcional):

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q tests/
```

**4. Primeiro teste rápido**, com uma live que esteja no ar agora: grava 3 minutos e gera os clipes do trecho.

```powershell
.venv\Scripts\python main.py --once --url https://kick.com/CANAL --max-duration 180
```

Na primeira execução, o modelo do Whisper é baixado automaticamente (~460 MB para o `small`). Num trecho curto pode não sair nenhum clipe, porque o detector precisa de alguns minutos de chat para formar a média. O teste valida a gravação e o chat. Para ver clipes, deixe gravar mais tempo.


**5. GPU NVIDIA para as legendas** (opcional, recomendado com placas RTX, ~1 GB):

```powershell
.venv\Scripts\python -m pip install -r requirements-gpu.txt
.venv\Scripts\python main.py --diagnostico
```

O app detecta sozinho o que a máquina tem (ADR-007):
- **Vídeo:** NVENC nas placas NVIDIA RTX/GTX, Quick Sync no vídeo integrado Intel, AMF nas AMD e CPU como último recurso.
- **Legendas:** CUDA na GPU NVIDIA, com o modelo `large-v3-turbo` se houver 6 GB de VRAM ou mais, e CPU caso contrário.

Se algo na GPU falhar, o clipe é refeito na CPU automaticamente. O `--diagnostico` explica cada escolha.

> **Linux/macOS:** instale `ffmpeg` pelo gerenciador de pacotes (`apt install ffmpeg` / `brew install ffmpeg`) e use `.venv/bin/python` no lugar de `.venv\Scripts\python`.
>
> **Motor openai-whisper** (opcional): `pip install openai-whisper` e `WHISPER_ENGINE = "openai"` no `config.py`. Ele puxa o PyTorch (~1 GB).

## Uso

1. Abra `config.py` e preencha `STREAM_URL`. Se quiser, ajuste `SPIKE_MULTIPLIER` e `WHISPER_MODEL`.
2. Rode:

```powershell
.venv\Scripts\python main.py
```

Nos exemplos abaixo, `python` = `.venv\Scripts\python`.

| Comando | Efeito |
|---|---|
| `python main.py` | monitora para sempre e processa cada live que acontecer |
| `python main.py --once` | encerra depois da primeira live |
| `python main.py --url https://kick.com/canal` | ignora a URL do `config.py` |
| `python main.py --process sessions/<pasta>` | refaz só os clipes de uma gravação já feita |
| `python main.py --max-duration 3600` | para de gravar depois de N segundos e processa o que gravou |
| `python main.py --url URL1 URL2` | acompanha várias lives ao mesmo tempo (até `MAX_PARALLEL_LIVES`) |
| `python main.py --diagnostico` | mostra a GPU detectada, o que vai rodar onde e um mini benchmark |

**Ctrl+C** durante a gravação para tudo de forma limpa e ainda gera os clipes do que foi gravado.

Os resultados ficam em `sessions/<data>_<plataforma>_<canal>/final/`, com os nomes `clip_01_seg0_12m30s.mp4` (posição no vídeo gravado). O log completo fica no `run.log` da mesma pasta.

## Interface gráfica

Para quem não quer mexer em terminal: cole o link da live e deixe rodar.

```powershell
.venv\Scripts\python gui.py
```

- Cole o link (Twitch, Kick ou YouTube) e clique em **Adicionar live**. Dá para acompanhar várias lives ao mesmo tempo (limite em `MAX_PARALLEL_LIVES`).
- Cada live vira um cartão com estado, tempo gravado, tamanho, mensagens do chat e picos. **Parar** encerra a gravação e o que já foi gravado vira clipes; **Abrir clipes** abre a pasta final.
- **Ajustes** muda o formato do clipe, o idioma da legenda, a sensibilidade dos picos e a pasta de saída. Os valores ficam em `settings.json`, ao lado do app.
- `python gui.py --demo` mostra a interface com lives simuladas, sem gravar nada.
- Sem FFmpeg instalado, a tela avisa e mostra o comando `winget install Gyan.FFmpeg`.

## Enviar clipes ao Google Drive

O app não pede login nem chave: ele só copia os clipes para a pasta do **Google Drive para desktop**, e o próprio Drive envia para a nuvem em segundo plano.

1. Instale o Google Drive para desktop (google.com/drive/download) e entre na sua conta.
2. No cartão da live, clique em **Enviar ▾ > Google Drive**, marque os clipes e confirme. Eles vão para `Meu Drive\Auto-Clipper\<pasta da live>\`. Os que já foram enviados aparecem desmarcados.
3. Para usar outra pasta (OneDrive, Dropbox, HD externo), vá em **Ajustes > Pasta do Google Drive** e clique em **Escolher…**. **Detectar** volta ao automático.

Para enviar clipes de lives antigas: **Abrir gravação** → escolha a pasta da live em `Vídeos\Auto-Clipper`.

Só os clipes finais são enviados; a gravação bruta, o áudio e as legendas ficam no computador.

## Postar no YouTube

O app pode postar os clipes direto no seu canal do YouTube (como Shorts). É opcional: sem conectar, tudo funciona como antes. Como o app não tem chave embutida, você cria uma credencial **sua** no Google Cloud (é grátis e leva uns 15 minutos, uma vez só).

1. Entre em **console.cloud.google.com** com a conta do Google do seu canal e crie um projeto novo, chamado `Auto-Clipper`.
2. No menu, vá em **APIs e serviços > Biblioteca**, procure **YouTube Data API v3** e clique em **Ativar**.
3. Configure a tela de consentimento (**Google Auth Platform**, nas abas **Branding** e **Público**):
   - Tipo de público: **Externo**. Preencha o nome do app (`Auto-Clipper`) e o seu e-mail.
   - Em **Público**, adicione o seu e-mail como **usuário de teste**.
   - Depois clique em **Publicar app** (fica "Em produção"). Sem isso o login expira a cada 7 dias e você teria de conectar de novo toda semana.
   - No primeiro login vai aparecer o aviso **"O Google não verificou este app"**. É normal: o app é seu. Clique em **Avançado** e depois em **Acessar Auto-Clipper (não seguro)**.
4. Em **Clientes**, clique em **Criar cliente**, escolha o tipo **App para computador** e baixe o arquivo **JSON**.
5. No Auto-Clipper, abra **Ajustes > YouTube > Conectar…**, escolha o JSON baixado e autorize no navegador que vai abrir. Quando aparecer "Login concluído", volte ao app: o status passa a **Conectado**.

Como usar:

- No cartão da live, clique em **Enviar ▾ > YouTube**. Marque os clipes, ajuste os títulos se quiser (a coluna **Título** é editável), escolha a visibilidade (**Público**, **Não listado** ou **Privado**) e clique em **Postar**. Os que já foram postados aparecem desmarcados, com o link; dê dois cliques para abrir.
- Pode fechar a janela de envio: os envios continuam em segundo plano e o cartão mostra o andamento. Fechar o app cancela o que ainda não terminou.
- **Postagem automática:** em **Ajustes > YouTube**, marque "Postar automaticamente cada clipe novo". Vem **desligada**. Ela só posta os clipes que forem criados depois que você liga a opção, nunca os antigos.
- O registro do que já foi postado fica em `youtube.json`, dentro da pasta de cada live, então o app nunca posta o mesmo clipe duas vezes, nem depois de reiniciar.

Limites e avisos:

- O YouTube dá uma cota de cerca de **100 envios por dia** para a sua credencial. Se acabar, o app avisa e você tenta de novo no dia seguinte.
- Se o YouTube deixar o vídeo como **privado** mesmo você pedindo público, o diálogo avisa. Projetos novos às vezes precisam de verificação do Google; você pode mudar a visibilidade no YouTube Studio.
- **Onde ficam os dados:** a credencial (`youtube_client_secret.json`) e o token do login (`youtube_token.json`) ficam em `%LOCALAPPDATA%\Auto-Clipper` (no executável) ou na pasta do projeto (código-fonte). Nada disso vai para o git nem para dentro do `.exe`: guarde esses arquivos como uma senha.
- **Desconectar:** **Ajustes > YouTube > Desconectar** apaga o token e revoga a permissão. Você também pode remover o acesso em myaccount.google.com/permissions.
- **Conteúdo de terceiros:** os clipes mostram a live de outra pessoa. Só poste com a **permissão** do streamer (e, de preferência, com crédito no título ou na descrição). Postar sem autorização pode gerar reivindicação de direitos autorais ou até a remoção do vídeo e advertências no seu canal. A responsabilidade pelo que é postado é sua.

## Executável (Windows, sem instalar nada)

Gere a pasta pronta para usar em qualquer PC Windows (ADR-009):

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt -r requirements-gpu.txt
.venv\Scripts\python build_exe.py
```

O resultado fica em `dist\Auto-Clipper\` (~2,8 GB, com FFmpeg, Streamlink, faster-whisper e CUDA embutidos). Copie a pasta para o outro PC e dê dois cliques em **`Auto-Clipper.exe`**. `auto-clipper-cli.exe --diagnostico` mostra a GPU e o encoder detectados. Os clipes vão para `Vídeos\Auto-Clipper` e os ajustes para `%LOCALAPPDATA%\Auto-Clipper`.

## Dicas

- **Clipes durante a live:** os clipes saem enquanto a live acontece, alguns minutos depois de cada momento (e já dá para abrir a pasta). Para voltar ao modo antigo (tudo só no fim), use `LIVE_CLIPPING = False` em `config.py`.
- **Formato do clipe:** `OUTPUT_LAYOUT = "vertical_blur"` (padrão: vídeo inteiro sobre fundo desfocado, nada da tela é perdido), `"vertical_crop"` (tela cheia, corta as laterais e costuma cortar a webcam) ou `"horizontal"` (16:9 como a live).
- **Tamanho da legenda:** os blocos têm no máximo `SUBTITLE_MAX_CHARS = 40` caracteres (~2 linhas) e `SUBTITLE_MAX_CUE_S = 3.0` segundos. `SUBTITLE_MAX_CHARS = 0` volta às frases inteiras do Whisper.
- **Live em outro idioma:** use `WHISPER_LANGUAGE = None` em `config.py` para o Whisper detectar o idioma. Forçar `"pt"` numa live em francês gera legenda vazia ou sem sentido.
- **Poucos ou muitos clipes:** ajuste `SPIKE_MULTIPLIER` (menor = mais sensível) e `MIN_MESSAGES_PER_BUCKET` (piso para chats pequenos).
- **Clipe começa tarde demais:** o chat reage depois do lance. Aumente `CLIP_PRE_S` ou `CHAT_DELAY_OFFSET_S`.
- **Tempo de processamento:** em CPU, cada clipe de 30 s leva ~40–50 s (faster-whisper `small` + 1 encode em 1080×1920 a 30 fps). O gargalo é o encode do vídeo: `VERTICAL_SIZE = (720, 1280)` ou `VIDEO_PRESET = "superfast"` aceleram bastante.
- **Disco:** ~3 GB por hora em `best`. Use `STREAM_QUALITY = "720p"` ou `DELETE_RAW_AFTER_PROCESS = True`.
- **FFmpeg recém-instalado:** se o terminal ainda não enxerga o `ffmpeg`, o programa encontra sozinho o atalho do winget.

## Estrutura

```
gui.py (interface)  main.py (CLI)  engine.py (várias lives + fila de clipes)
hardware.py (GPU)  config.py  session.py  recorder.py  chat_listener.py
hype_detector.py  clipper.py  transcriber.py  exporter.py (Drive)  uploader.py (YouTube)
tests/  docs/adr/
requirements.txt  requirements-gpu.txt  requirements-dev.txt
```

Detalhes em [ARCHITECTURE.md](ARCHITECTURE.md).
