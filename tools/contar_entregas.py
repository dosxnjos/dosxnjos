"""Conta os PRs juntados (merged) num ano em todos os repositórios de uma organização do GitHub.

Uso:
    python tools/contar_entregas.py <org> [ano] [--excluir repo1,repo2]

- Precisa do `gh` logado numa conta com acesso aos repositórios privados da organização.
  A contagem é manual: nenhuma credencial da organização entra neste repositório.
- Conta só PR aberto por pessoa (`user.type == "User"`); PR aberto por robô (`Bot`: dependências,
  agente automático, rotinas de dados) fica fora. `--excluir` tira repositórios de ensaio.
- A busca do GitHub devolve no máximo 1.000 itens por consulta: a conta é fatiada por mês e a soma
  é conferida contra o total do ano.
"""
import argparse
import calendar
import json
import subprocess
import sys
from collections import Counter

LIMITE_BUSCA = 1000


def gh_busca(consulta, paginar):
    """Itens da busca de issues/PRs (repo, tipo do autor) e o total_count da primeira página."""
    url = f"search/issues?q={consulta}&per_page=100"
    cmd = ["gh", "api", url] + (["--paginate"] if paginar else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:   # limite da busca: 30 consultas por minuto; espere 1 min e rode de novo
        sys.exit(f"gh api falhou: {r.stderr.strip()[:300]}")
    total, itens = None, []
    dec = json.JSONDecoder()
    texto, pos = r.stdout.strip(), 0
    while pos < len(texto):   # com --paginate o gh emite um JSON por página, colados
        pagina, pos = dec.raw_decode(texto, pos)
        while pos < len(texto) and texto[pos].isspace():
            pos += 1
        if total is None:
            total = pagina["total_count"]
        itens += [(i["repository_url"].rsplit("/", 1)[-1].lower(), i["user"]["type"], i["user"]["login"].lower())
                  for i in pagina["items"]]
    return total or 0, itens


def main(argv):
    p = argparse.ArgumentParser(description="Conta PRs juntados num ano numa organização do GitHub.")
    p.add_argument("org")
    p.add_argument("ano", nargs="?", type=int, default=2026)
    p.add_argument("--excluir", default="", help="repositórios fora da conta, separados por vírgula")
    p.add_argument("--autores", default="", help="só PRs destas contas (as suas), separadas por vírgula")
    a = p.parse_args(argv)
    org, ano = a.org, a.ano
    excluir = set(filter(None, a.excluir.lower().split(",")))
    autores = set(filter(None, a.autores.lower().split(",")))
    base = f"org:{org}+is:pr+is:merged"

    total_ano, _ = gh_busca(f"{base}+merged:{ano}-01-01..{ano}-12-31", paginar=False)
    if total_ano == 0:
        sys.exit("a busca não achou nenhum PR: o `gh` está na conta que enxerga os repositórios privados?")
    itens = []
    for mes in range(1, 13):
        fim = calendar.monthrange(ano, mes)[1]
        total_mes, do_mes = gh_busca(f"{base}+merged:{ano}-{mes:02d}-01..{ano}-{mes:02d}-{fim:02d}", paginar=True)
        if total_mes >= LIMITE_BUSCA or len(do_mes) != total_mes:
            sys.exit(f"mês {mes:02d}: a busca trouxe {len(do_mes)} de {total_mes}; fatie mais fino")
        itens += do_mes
    if len(itens) != total_ano:
        sys.exit(f"a soma dos meses ({len(itens)}) não bate com o total do ano ({total_ano})")

    repos = {r for r, _, _ in itens}
    sumidos = excluir - repos
    if sumidos:
        sys.exit(f"--excluir com repositório que não apareceu na busca: {', '.join(sorted(sumidos))}")
    humanos = [(r, login) for r, t, login in itens if t == "User"]
    print(f"PRs juntados em {ano}: {total_ano}")
    print(f"  abertos por robô (fora): {total_ano - len(humanos)}")
    print(f"  de repositórios excluídos (fora): {sum(1 for r, _ in humanos if r in excluir)}")
    humanos = [(r, login) for r, login in humanos if r not in excluir]
    por_login = Counter(login for _, login in humanos)
    for login, n in por_login.most_common():
        print(f"    aberto por {login}: {n}" + ("" if not autores or login in autores else " (fora: não é seu)"))
    if not autores and len(por_login) > 1:
        print("  atenção: mais de uma conta abriu PR; use --autores com as suas")
    print(f"  entregas: {sum(n for login, n in por_login.items() if not autores or login in autores)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv[1:])
