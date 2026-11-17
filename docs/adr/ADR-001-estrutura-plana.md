# ADR-001: Estrutura plana com uma única porta (ChatSource)

**Status:** Aceito · **Data:** 2026-11-17

## Contexto
O pedido exige "arquivos simples dentro da mesma pasta". As skills de arquitetura sugerem dois extremos: um layout `src/` com camadas (python-project-structure) ou Clean/Hexagonal com portas para tudo (architecture-patterns). O projeto tem ~7 módulos e um único usuário.

## Opções
- **A: Plano, sem abstrações.** Simples, mas o `if plataforma == ...` acaba espalhado pelo código de chat.
- **B: Plano + porta só onde há variação real (`ChatSource`).** É o meio-termo.
- **C: Hexagonal completo (domain/ports/adapters).** Testabilidade máxima, mas excessivo para o tamanho do projeto e contra o pedido.

## Decisão
**B.** Módulos na raiz, uma responsabilidade por arquivo, `__all__` explícito, imports absolutos. A lógica de decisão (`hype_detector`, `clipper.plan_clips`) fica em funções puras, o que dá a testabilidade do hexagonal sem as camadas.

## Consequências
- Para adicionar uma plataforma basta escrever um adaptador `ChatSource` e uma linha em `make_chat_source`.
- Se o projeto crescer (UI, fila de jobs), migrar para `src/auto_clipper/` é mecânico.
