# ADR-012: Postar clipes no YouTube (Shorts) pela API oficial

**Status:** Aceito · **Data:** 2026-11-18

## Contexto
O usuário quer que o app poste os clipes no YouTube e no TikTok. A pesquisa nas documentações oficiais (2026-11-18) mostrou:

- **YouTube Data API v3** (`videos.insert`):
  - gratuita;
  - cota própria de **100 envios por dia** (1 unidade cada);
  - pela página atual do método, vídeos enviados por projetos não verificados **não ficam presos no modo privado**.

  Precisa de um projeto no Google Cloud e de um cliente OAuth "App para computador" criado pelo próprio usuário. Com o projeto em modo "Teste", o login (refresh token) expira em 7 dias. Em "Em produção", sem verificação, o Google só mostra o aviso "app não verificado" no login.
- **TikTok Content Posting API:** exige revisão do app, e postar público exige uma auditoria sem prazo publicado. Fica para depois (ADR futuro).

## Decisão
Implementar a postagem no YouTube com a API oficial, com uma **credencial do próprio usuário**.

- **Exceção consciente ao "sem contas nem chaves" (ADR-001):**
  - A postagem é **opcional**. Sem conectar, o app funciona exatamente como antes.
  - O arquivo de cliente OAuth (`client_secret*.json`) **é do usuário**. Ele o cria no Google Cloud e o escolhe em Ajustes, e o app copia o arquivo para `runtime.data_dir()`.
  - Nada é embutido no `.exe` e nada vai para o git.
  - O token fica em `data_dir()/youtube_token.json`.
- **Escopo mínimo:** `https://www.googleapis.com/auth/youtube.upload`.
- **Dependências:**
  - `google-auth-oauthlib` faz o login: fluxo de "app instalado" com redirect em loopback e porta aleatória.
  - O **upload retomável** é implementado à mão sobre `google.auth.transport.requests.AuthorizedSession`: POST de início, PUT em blocos, 308 com `Range`, novas tentativas com backoff em 5xx e falha de rede.
  - **Não** usamos `google-api-python-client`, que é pesado (documentos de discovery de todas as APIs) e cresceria o `.exe` à toa.
- **Novo módulo `uploader.py`**, sem Qt:
  - `YouTubeClient` (conectar, desconectar, enviar);
  - `UploadQueue` (uma thread, um envio por vez);
  - registro do que já foi postado em `<sessão>/youtube.json`, para nunca postar duas vezes, nem depois de reiniciar o app;
  - modelos de título e descrição em `config.py`.
- **Erros tipados:**
  - `AuthError`: login expirou ou foi revogado. O token é apagado e a GUI pede para reconectar.
  - `QuotaError`: cota diária esgotada. A fila é esvaziada e avisa "tente amanhã".
  - `UploadError`: demais falhas.
- **GUI:**
  - o botão "Enviar ao Drive" do cartão vira **"Enviar ▾"**, com o menu Google Drive / YouTube;
  - o diálogo do YouTube lista os clipes com caixa de seleção e título editável, mostra "já postado" com link, tem a visibilidade (Público / Não listado / Privado) e o progresso;
  - em Ajustes, a seção YouTube tem conectar/desconectar, visibilidade padrão e "Postar automaticamente cada clipe novo" (**desligado** por padrão).
- **Postagem automática:** é a GUI que observa `JobStatus.final_clips` crescer e põe os clipes novos na `UploadQueue`. O `engine.py` continua sem conhecer o uploader (mesma fronteira do ADR-011).

## Consequências
- O usuário faz uma configuração única de ~15 min no Google Cloud (passo a passo no README).
- Em modo "Teste", é preciso reconectar a cada 7 dias. O README orienta a publicar o projeto ("Em produção").
- A cota de 100 envios por dia sobra para o uso normal. Com ela esgotada, o app avisa e os clipes podem ser reenviados no dia seguinte pelo diálogo.
- Se o Google voltar a travar projetos não verificados no modo privado, os vídeos sairão privados e o usuário terá de pedir a auditoria. O diálogo mostra a visibilidade que o YouTube devolveu.
