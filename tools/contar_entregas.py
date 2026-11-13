"""Conta os PRs juntados (merged) num ano numa organização do GitHub, por categoria de origem.

Uso:
    python tools/contar_entregas.py <org> [ano] --autores <minhas,contas> [--agentes <apps>]
        [--excluir <repos>] [--meu-nome <nome>] [--json]

Precisa do `gh` logado numa conta com acesso aos repositórios privados da organização. A contagem é manual:
nenhuma credencial da organização entra neste repositório.

Categorias (a primeira regra que casar vence):
  fora        repositório em --excluir (ensaio)
  manutencao  atualização de dependência (dependabot/renovate) ou CHANGELOG regenerado por robô
  dados       rotina de dados (rótulo `dados` ou ramo `dados/`)
  conserto    a cadeia de origem tem issue automática: rótulo `erro-automatico`/`zelador`, aberta por robô que não
              é o agente, ou título de alerta (`main vermelha`, `Alerta:`, `Trava:`, `Teste instável`,
              `Publicação parou`, `Erro automático`); subdividido em `trava` (zelador) e `erro` (o resto)
  pedido      a cadeia tem `pedido de: <pessoa>` de outra pessoa, ou a issue raiz é de outra conta humana
  agente      dirigida por mim, feita pelo agente do GitHub a partir de uma issue
  conversa    dirigida por mim, sem issue: PR aberto pelas minhas contas ou pelo agente numa sessão minha
  sem         o resto
Cadeia de origem: issues ligadas ao PR, o número do ramo `claude/issue-<n>`/`claude/arquitetura-<n>` e, de cada
issue, a linha `Origem: #<n>` (ou o rodapé `aberta pelo arquiteto ..., issue #<n>`), até 5 saltos.
A busca do GitHub devolve no máximo 1.000 itens por consulta: a coleta é fatiada por mês e conferida.
"""
import argparse
import calendar
import collections
import json
import re
import subprocess
import sys

CONSULTA_PRS = """query($q: String!, $cur: String) { search(query: $q, type: ISSUE, first: 50, after: $cur) {
  issueCount pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { number title headRefName mergedAt repository { name }
    author { login __typename } labels(first: 20) { nodes { name } }
    closingIssuesReferences(first: 5) { nodes { number } } } } } }"""
ORIGEM = re.compile(r"(?im)^\s*-?\s*Origem:\s*(?:([\w.-]+/[\w.-]+))?#(\d+)|aberta pelo arquiteto[^\n]*issue #(\d+)")
TITULO_AUTO = re.compile(r"(?i)^(main vermelha|alerta:|trava:|teste instável|publicação parou|erro automático)")
PEDIDO = re.compile(r"(?im)^\s*\**pedido de:\**\s*([a-zà-ú]+)")
ROBOS_MANUTENCAO = {"dependabot", "renovate"}


def gh_graphql(consulta, parcial=False, **variaveis):
    """`parcial`: aceita resposta com erro em parte (número citado que não existe vira nó vazio)."""
    cmd = ["gh", "api", "graphql", "-f", f"query={consulta}"]
    for k, v in variaveis.items():
        if v is not None:
            cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    try:
        dados = json.loads(r.stdout or "{}").get("data")
    except json.JSONDecodeError:
        dados = None
    if r.returncode != 0 and not (parcial and dados):   # limite da API: espere um pouco e rode de novo
        sys.exit(f"gh api graphql falhou: {r.stderr.strip()[:300]}")
    return dados


def coletar_prs(org, ano):
    prs = []
    for mes in range(1, 13):
        fim = calendar.monthrange(ano, mes)[1]
        q = f"org:{org} is:pr is:merged merged:{ano}-{mes:02d}-01..{ano}-{mes:02d}-{fim:02d}"
        cur, do_mes = None, []
        while True:
            s = gh_graphql(CONSULTA_PRS, q=q, cur=cur)["search"]
            do_mes += [n for n in s["nodes"] if n]
            if not s["pageInfo"]["hasNextPage"]:
                break
            cur = s["pageInfo"]["endCursor"]
        if s["issueCount"] >= 1000 or len(do_mes) != s["issueCount"]:
            sys.exit(f"mês {mes:02d}: a busca trouxe {len(do_mes)} de {s['issueCount']}; fatie mais fino")
        prs += do_mes
    return prs


def coletar_issues(org, chaves, cache):
    faltam = [k for k in dict.fromkeys(chaves) if k not in cache]
    for i in range(0, len(faltam), 40):
        lote, partes = faltam[i:i + 40], []
        for j, k in enumerate(lote):
            repo, n = k.split("#")
            partes.append(f'i{j}: repository(owner: "{org}", name: "{repo}") {{ issueOrPullRequest(number: {n}) {{'
                          " ... on Issue { title body author { login __typename } labels(first: 20) { nodes { name } } }"
                          " ... on PullRequest { title body author { login __typename } } } }")
        dados = gh_graphql("{" + " ".join(partes) + "}", parcial=True)
        for j, k in enumerate(lote):
            no = (dados.get(f"i{j}") or {}).get("issueOrPullRequest") or {}
            autor = no.get("author") or {}
            cache[k] = {"titulo": no.get("title", ""), "corpo": no.get("body") or "",
                        "autor": autor.get("login", ""), "robo": autor.get("__typename") == "Bot",
                        "rotulos": [x["name"] for x in (no.get("labels") or {}).get("nodes", [])]}


def origens(k, cache):
    """A issue `k` e as de cima pela linha Origem, até 5 saltos."""
    cadeia = [k]
    for _ in range(5):
        m = ORIGEM.search(cache.get(cadeia[-1], {}).get("corpo", ""))
        if not m:
            break
        repo = (m.group(1) or cadeia[-1].split("#")[0]).split("/")[-1]
        prox = f"{repo}#{m.group(2) or m.group(3)}"
        if prox in cadeia:
            break
        cadeia.append(prox)
    return cadeia


def chaves_do_pr(p):
    repo = p["repository"]["name"]
    ks = [f"{repo}#{i['number']}" for i in p["closingIssuesReferences"]["nodes"]]
    m = re.match(r"claude/(?:issue|arquitetura)-(\d+)", p["headRefName"])
    return list(dict.fromkeys(ks + ([f"{repo}#{m.group(1)}"] if m else [])))


def classificar(p, cache, a):
    """(categoria, detalhe)."""
    rotulos = {x["name"] for x in p["labels"]["nodes"]}
    autor = (p["author"] or {}).get("login", "").lower()
    ramo, titulo = p["headRefName"], p["title"]
    if p["repository"]["name"].lower() in a.excluir:
        return "fora", "ensaio"
    if autor in ROBOS_MANUTENCAO or rotulos & {"dependencias", "dependencies"}:
        return "manutencao", "dependência"
    if ramo.startswith("bot/changelog") or titulo.startswith("CHANGELOG.md regenerado"):
        return "manutencao", "changelog"
    if "dados" in rotulos or ramo.startswith("dados/"):
        nome = ramo.split("/")[1] if ramo.startswith("dados/") else "rótulo"
        return "dados", "carimbo de data" if nome[:1].isdigit() else nome
    ks = chaves_do_pr(p)
    for k in ks:
        cadeia = origens(k, cache)
        for x in cadeia:
            i = cache.get(x, {})
            robo_externo = i.get("robo") and i.get("autor", "").lower() not in a.agentes
            if robo_externo or {"erro-automatico", "zelador"} & set(i.get("rotulos", [])) \
                    or TITULO_AUTO.match(i.get("titulo", "")):
                trava = "zelador" in i.get("rotulos", []) or i.get("titulo", "").startswith("Trava:")
                return "conserto", "trava" if trava else "erro"
        for x in cadeia:
            m = PEDIDO.search(cache.get(x, {}).get("corpo", ""))
            if m and m.group(1).lower() != a.meu_nome:
                return "pedido", m.group(1).lower()
        raiz = cache.get(cadeia[-1], {})
        if raiz.get("autor") and not raiz.get("robo") and raiz["autor"].lower() not in a.autores:
            return "pedido", raiz["autor"]
    if ks:
        return "agente", "issue"
    if autor in a.autores or autor in a.agentes:
        return "conversa", "conta" if autor in a.autores else "credencial do agente"
    return "sem", autor


def main(argv):
    p = argparse.ArgumentParser(description="PRs juntados num ano numa organização, por categoria de origem.")
    p.add_argument("org")
    p.add_argument("ano", nargs="?", type=int, default=2026)
    p.add_argument("--autores", required=True, help="as minhas contas, separadas por vírgula")
    p.add_argument("--agentes", default="", help="logins dos Apps do agente (sem o sufixo [bot])")
    p.add_argument("--excluir", default="", help="repositórios fora da conta (ensaio), separados por vírgula")
    p.add_argument("--meu-nome", default="gabriel", help="o meu nome na linha 'pedido de:'")
    p.add_argument("--json", action="store_true", help="PR a PR, com a categoria")
    a = p.parse_args(argv)
    lista = lambda s: set(filter(None, s.lower().split(",")))   # noqa: E731
    a.autores, a.agentes, a.excluir, a.meu_nome = lista(a.autores), lista(a.agentes), lista(a.excluir), a.meu_nome.lower()

    prs = coletar_prs(a.org, a.ano)
    if not prs:
        sys.exit("a busca não achou nenhum PR: o `gh` está na conta que enxerga os repositórios privados?")
    sumidos = a.excluir - {x["repository"]["name"].lower() for x in prs}
    if sumidos:
        sys.exit(f"--excluir com repositório que não apareceu na busca: {', '.join(sorted(sumidos))}")
    cache, fila = {}, [k for x in prs for k in chaves_do_pr(x)]
    for _ in range(6):
        coletar_issues(a.org, fila, cache)
        fila = [c for k in fila for c in origens(k, cache) if c not in cache]
        if not fila:
            break

    res = [(x, *classificar(x, cache, a)) for x in prs]
    if a.json:
        json.dump([{"repo": x["repository"]["name"], "pr": x["number"], "juntado": x["mergedAt"], "categoria": c,
                    "detalhe": d} for x, c, d in res], sys.stdout, ensure_ascii=False, indent=1)
        return
    cat = collections.Counter(c for _, c, _ in res)
    det = collections.Counter((c, d) for _, c, d in res)
    print(f"PRs juntados em {a.ano}: {len(prs)}")
    for c in ("conversa", "agente", "pedido", "conserto", "manutencao", "dados", "fora", "sem"):
        partes = ", ".join(f"{d or '-'} {n}" for (cc, d), n in det.most_common() if cc == c)
        print(f"  {c:11} {cat[c]:5}  ({partes})" if cat[c] else f"  {c:11} {0:5}")
    print(f"  dirigidas por mim (conversa + agente): {cat['conversa'] + cat['agente']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv[1:])
