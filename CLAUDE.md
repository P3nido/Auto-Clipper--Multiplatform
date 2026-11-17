# Auto-Clipper Multiplataforma — guia para agentes

Projeto Python **local e gratuito** (sem contas, tokens ou chaves de API) que grava lives da Twitch, YouTube ou Kick, detecta picos de chat e gera clipes legendados. A arquitetura completa está em `ARCHITECTURE.md` e as decisões estão em `docs/adr/`. **Leia os dois antes de mudar o design.**

## Fluxo de trabalho com subagentes
- **Orquestrador (sessão principal):** planeja, define contratos, revisa diffs e faz os commits.
- **`coder` (Sonnet):** implementa um módulo por vez a partir do esqueleto e dos testes.
- **`qa-explorer` (Haiku):** valida sintaxe, testes, dependências, ferramentas e logs. Não edita nada.
- Ordem das fases: ARCHITECTURE.md §11. Uma fase só começa quando a anterior estiver com os testes verdes.

## Convenções
- Layout **plano**: os módulos ficam na raiz e os imports são absolutos (`import config`, `from session import Session`). Não crie pacotes ou subpastas de código sem um ADR.
- Cada módulo declara `__all__`. Assinaturas públicas são contrato: para mudar uma, atualize o ARCHITECTURE.md §3 e os testes junto.
- Os módulos de etapa não importam uns aos outros (exceto `clipper` importar o tipo `Spike`). Quem orquestra é o `main.py`.
- Toda configuração fica em `config.py`. Não use valores mágicos no código.
- Use `logging` e não `print`. Subprocess sempre com lista de argumentos, nunca `shell=True`.
- Relógio único: `session.now()`. Conversão de tempo do chat para tempo do vídeo: só via `RecordingSegment.wall_to_offset()`.
- Docs e comentários em português. Identificadores em inglês.
- Commits no formato Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Comandos
```bash
pip install -r requirements-dev.txt
python -m pytest -q tests/
python main.py --once
python main.py --process sessions/<pasta>
```

## Ferramentas externas
FFmpeg/FFprobe no PATH (obrigatório). Chrome ou Chromium para Kick.
