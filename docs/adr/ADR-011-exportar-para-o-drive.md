# ADR-011: Enviar clipes ao Google Drive pela pasta sincronizada

**Status:** Aceito · **Data:** 2026-11-18

## Contexto
O usuário quer escolher clipes dentro do app e mandá-los para o Google Drive, para acessar de qualquer lugar. Houve três caminhos possíveis:

1. **API do Google Drive (OAuth):** exige um projeto no Google Cloud, credenciais embutidas no `.exe`, a tela "app não verificado" no primeiro login, guardar e renovar tokens e fazer upload retomável. Isso quebra a premissa do projeto: "local, gratuito, sem contas, tokens ou chaves".
2. **Envio manual pelo site:** funciona, mas é cansativo com dezenas de clipes espalhados em várias pastas de sessão.
3. **Pasta sincronizada pelo Google Drive para desktop:** o programa oficial e gratuito do Google monta uma unidade (por exemplo, `G:\Meu Drive`). Tudo que é copiado para lá sobe sozinho, com novas tentativas e tolerância a rede instável.

## Decisão
Usar a pasta sincronizada (opção 3). O app **só copia arquivos**; quem envia para a nuvem é o Drive para desktop.

- Novo módulo `exporter.py`, sem Qt:
  - `detect_drive_folder()` procura a raiz "Meu Drive"/"My Drive";
  - `resolve_export_dir()` aplica o ajuste do usuário;
  - `export_clips()` copia os arquivos.
- **Destino:** `<pasta do Drive>/<EXPORT_SUBFOLDER>/<nome da pasta da sessão>/clip_XX….mp4`.
- **Só os clipes finais** (`final/*.mp4`). Gravações brutas (`.ts`), áudio e legendas nunca são copiados, porque teriam GBs e lotariam o Drive e a internet.
- **Cópia segura:** escreve em `<nome>.part` e só depois renomeia para o nome final, para o Drive não subir um arquivo pela metade. Um arquivo que já existe no destino com o mesmo tamanho conta como "já enviado" e é pulado, então reenviar não duplica. Um nome igual com tamanho diferente ganha o sufixo ` (2)`.
- **Ajuste `export_dir`:**
  - `"auto"` (padrão) detecta o Drive para desktop;
  - qualquer outro caminho funciona também, como OneDrive, Dropbox ou um HD externo.
- **GUI:**
  - cada cartão ganha o botão "Enviar ao Drive";
  - uma janela lista os clipes com caixas de seleção e marca os que já estão no destino;
  - a cópia roda em uma thread, com barra de progresso.
- **Fronteira:** `engine.py` não conhece o exportador. Só a GUI (cliente) o chama, com a lista `JobStatus.final_clips`.

## Consequências
- Não há login, token nem dependência nova. A premissa do projeto (ADR-001) se mantém.
- O usuário precisa instalar o Google Drive para desktop em cada máquina, ou escolher outra pasta em Ajustes.
- O app não sabe quando o upload termina na nuvem. Ele informa "copiado; o Drive envia em segundo plano".
- Arquivos no "Meu Drive" em modo streaming continuam ocupando o disco local até subirem, porque o Drive para desktop usa cache. São só os clipes escolhidos, de ~10–30 MB cada.
