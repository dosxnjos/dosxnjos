"""Conta as entregas numa organização do GitHub por categoria de origem, por mês, por ano e desde o início.

Uso:
    python tools/contar_entregas.py <org> --autores <minhas,contas> --emails <meus,emails>
        [--agentes <apps>] [--excluir <repos>] [--meu-nome <nome>] [--inicio-central AAAA-MM-DD]
        [--sem-commits] [--json]

Precisa do `gh` logado numa conta com acesso aos repositórios privados da organização, e do `git`. A contagem é
manual: nenhuma credencial da organização entra neste repositório.

Unidade de entrega:
  - PR juntado (merged), a partir de quando o trabalho passou a entrar por PR;
  - antes disso (e em repo que ainda commita direto), um "lote": os commits diretos na branch padrão do mesmo
    repo, no mesmo dia e da mesma categoria; na categoria `central`, um lote por card. Commit que o GitHub gravou
    ao juntar um PR (committer noreply@github.com) já conta como PR e sai daqui.

Categorias (a primeira regra que casar vence):
  fora        repositório em --excluir (ensaio, anotações)
  manutencao  dependência (dependabot/renovate) ou CHANGELOG regenerado por robô
  dados       rotina de dados (rótulo `dados`, ramo `dados/`, ou commit direto de robô)
  conserto    a cadeia de origem tem issue automática: rótulo `erro-automatico`/`zelador`, aberta por robô que não
              é o agente, ou título de alerta (`main vermelha`, `Alerta:`, `Trava:`, `Teste instável`,
              `Publicação parou`, `Erro automático`); detalhe `trava` (zelador) ou `erro`
  pedido      a cadeia tem `pedido de: <pessoa>` de outra pessoa, ou a issue raiz é de outra conta humana
  agente      dirigida por mim, feita pelo agente do GitHub a partir de uma issue
  central     dirigida por mim, feita por executor aberto por IA: id de card (`c`/`i` + 12 hex) no ramo, no
              título ou na mensagem do commit (a partir de --inicio-central)
  conversa    dirigida por mim, numa conversa aberta: o resto do que saiu das minhas contas
  sem         o resto
Cadeia de origem: issues ligadas ao PR, o número do ramo `claude/issue-<n>`/`claude/arquitetura-<n>` e, de cada
issue, a linha `Origem: #<n>` (ou o rodapé `aberta pelo arquiteto ..., issue #<n>`), até 5 saltos.
"""
import argparse
import collections
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

CONSULTA_PRS = """query($q: String!, $cur: String) { search(query: $q, type: ISSUE, first: 50, after: $cur) {
  issueCount pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { number title headRefName mergedAt repository { name }
    author { login __typename } labels(first: 20) { nodes { name } }
    closingIssuesReferences(first: 5) { nodes { number } } } } } }"""
ORIGEM = re.compile(r"(?im)^\s*-?\s*Origem:\s*(?:([\w.-]+/[\w.-]+))?#(\d+)|aberta pelo arquiteto[^\n]*issue #(\d+)")
TITULO_AUTO = re.compile(r"(?i)^(main vermelha|alerta:|trava:|teste instável|publicação parou|erro automático)")
PEDIDO = re.compile(r"(?im)^\s*\**pedido de:\**\s*([a-zà-ú]+)")
CARD = re.compile(r"\b([ci][0-9a-f]{12})\b|\bcard[ `]+([a-z][\w-]*\d[\w-]*)")
ROBOS_MANUTENCAO = {"dependabot", "renovate"}
ORDEM = ("conversa", "central", "agente", "pedido", "conserto", "manutencao", "dados", "fora", "sem")
DIRIGIDAS = ("conversa", "central", "agente")
FUSO = datetime.timezone(datetime.timedelta(hours=-3))


def rodar(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def gh_graphql(consulta, parcial=False, **variaveis):
    """`parcial`: aceita resposta com erro em parte (número citado que não existe vira nó vazio)."""
    cmd = ["gh", "api", "graphql", "-f", f"query={consulta}"]
    for k, v in variaveis.items():
        if v is not None:
            cmd += ["-f", f"{k}={v}"]
    r = rodar(cmd)
    try:
        dados = json.loads(r.stdout or "{}").get("data")
    except json.JSONDecodeError:
        dados = None
    if r.returncode != 0 and not (parcial and dados):   # limite da API: espere um pouco e rode de novo
        sys.exit(f"gh api graphql falhou: {r.stderr.strip()[:300]}")
    return dados


def meses(inicio, fim):
    a, m = inicio
    while (a, m) <= fim:
        yield a, m
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)


def coletar_prs(org, inicio, fim):
    prs = []
    for ano, mes in meses(inicio, fim):
        ult = (datetime.date(ano + (mes == 12), mes % 12 + 1, 1) - datetime.timedelta(days=1)).day
        q = f"org:{org} is:pr is:merged merged:{ano}-{mes:02d}-01..{ano}-{mes:02d}-{ult:02d}"
        cur, do_mes = None, []
        while True:
            s = gh_graphql(CONSULTA_PRS, q=q, cur=cur)["search"]
            do_mes += [n for n in s["nodes"] if n]
            if not s["pageInfo"]["hasNextPage"]:
                break
            cur = s["pageInfo"]["endCursor"]
        if s["issueCount"] >= 1000 or len(do_mes) != s["issueCount"]:
            sys.exit(f"{ano}-{mes:02d}: a busca trouxe {len(do_mes)} de {s['issueCount']}; fatie mais fino")
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


def card(texto, dia, a):
    m = CARD.search(texto)
    return (m.group(1) or m.group(2)) if m and (not a.inicio_central or dia >= a.inicio_central) else ""


def classificar_pr(p, cache, a, dia):
    """(categoria, detalhe)."""
    rotulos = {x["name"] for x in p["labels"]["nodes"]}
    autor = (p["author"] or {}).get("login", "").lower()
    ramo, titulo = p["headRefName"], p["title"]
    if p["repository"]["name"].lower() in a.excluir:
        return "fora", "excluído"
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
        c = card(f"{ramo} {titulo}", dia, a)
        return ("central", c) if c else ("conversa", "conta" if autor in a.autores else "credencial do agente")
    return "sem", autor


def lotes_diretos(org, repos, a):
    """Commits diretos na branch padrão, agrupados em lotes: [(mês, categoria, detalhe)]."""
    sep, lotes = "\x1f", set()
    with tempfile.TemporaryDirectory(prefix="entregas-") as base:
        for repo in repos:
            d = os.path.join(base, repo)
            r = rodar(["git", "clone", "-q", "--bare", "--filter=blob:none", f"https://github.com/{org}/{repo}.git", d])
            if r.returncode != 0:
                sys.exit(f"git clone de {repo} falhou: {r.stderr.strip()[:200]}")
            # %at (instante) e o dia em Brasília calculado aqui: o TZ do git varia com a máquina (no Windows, Londres)
            r = rodar(["git", "-C", d, "log", "HEAD", "--first-parent", "--no-merges",
                       f"--format=%ae{sep}%ce{sep}%at{sep}%s{sep}%b%x1e"])
            for reg in r.stdout.split("\x1e"):
                campos = (reg.strip("\n").split(sep) + [""] * 5)[:5]
                ae, ce, instante, assunto, corpo = campos[0].lower(), campos[1].lower(), campos[2], campos[3], campos[4]
                dia = datetime.datetime.fromtimestamp(int(instante), FUSO).strftime("%Y-%m-%d") \
                    if instante.isdigit() else ""
                if not dia or ce == "noreply@github.com" or repo.lower() in a.excluir:
                    continue   # vazio, PR juntado pelo GitHub (já contado) ou repo fora
                if "[bot]" in ae + ce or ae.endswith("@users.noreply.github.com"):
                    cat = "manutencao" if re.search(r"(?i)changelog|depend", assunto) else "dados"
                    lotes.add((repo, dia, cat, ""))
                elif ae in a.emails or ce in a.emails:
                    c = card(f"{assunto}\n{corpo}", dia, a)
                    lotes.add(("*", "", "central", c) if c else (repo, dia, "conversa", ""))   # card: um lote só
                    if c:
                        lotes.add(("__card_dia", c, dia, ""))
                else:
                    lotes.add((repo, dia, "sem", ""))
    primeiro_dia = {}
    for x in lotes:
        if x[0] == "__card_dia":
            primeiro_dia[x[1]] = min(primeiro_dia.get(x[1], x[2]), x[2])
    saida = []
    for repo, dia, cat, c in lotes:
        if repo == "__card_dia":
            continue
        dia = primeiro_dia.get(c, dia) if cat == "central" else dia
        saida.append((dia[:7], cat, "card" if cat == "central" else "lote"))
    return saida


def main(argv):
    p = argparse.ArgumentParser(description="Entregas numa organização por categoria, mês, ano e desde o início.")
    p.add_argument("org")
    p.add_argument("--autores", required=True, help="as minhas contas do GitHub, separadas por vírgula")
    p.add_argument("--emails", default="", help="os meus e-mails de commit (para os commits diretos)")
    p.add_argument("--agentes", default="", help="logins dos Apps do agente (sem o sufixo [bot])")
    p.add_argument("--excluir", default="", help="repositórios fora da conta, separados por vírgula")
    p.add_argument("--meu-nome", default="gabriel", help="o meu nome na linha 'pedido de:'")
    p.add_argument("--inicio-central", default="", help="AAAA-MM-DD: id de card só conta a partir daqui")
    p.add_argument("--sem-commits", action="store_true", help="só PRs (pula os commits diretos)")
    p.add_argument("--json", action="store_true", help="série mensal e PR a PR, com a categoria")
    a = p.parse_args(argv)
    lista = lambda s: set(filter(None, s.lower().split(",")))   # noqa: E731
    a.autores, a.agentes, a.excluir, a.emails = lista(a.autores), lista(a.agentes), lista(a.excluir), lista(a.emails)
    a.meu_nome = a.meu_nome.lower()

    r = rodar(["gh", "api", "--paginate", f"orgs/{a.org}/repos?per_page=100", "-q", ".[] | [.name, .created_at] | @tsv"])
    repos = dict(line.split("\t") for line in r.stdout.splitlines() if "\t" in line)
    if not repos:
        sys.exit("não vi nenhum repositório: o `gh` está na conta que enxerga os repositórios privados?")
    sumidos = a.excluir - {x.lower() for x in repos}
    if sumidos:
        sys.exit(f"--excluir com repositório que não existe na organização: {', '.join(sorted(sumidos))}")
    criado = min(repos.values())
    hoje = datetime.datetime.now(FUSO)
    prs = coletar_prs(a.org, (int(criado[:4]), int(criado[5:7])), (hoje.year, hoje.month))
    cache, fila = {}, [k for x in prs for k in chaves_do_pr(x)]
    for _ in range(6):
        coletar_issues(a.org, fila, cache)
        fila = [c for k in fila for c in origens(k, cache) if c not in cache]
        if not fila:
            break

    itens, por_pr = [], []
    for x in prs:
        juntado = datetime.datetime.fromisoformat(x["mergedAt"].replace("Z", "+00:00")).astimezone(FUSO)
        c, d = classificar_pr(x, cache, a, juntado.strftime("%Y-%m-%d"))
        itens.append((juntado.strftime("%Y-%m"), c, "pr"))
        por_pr.append({"repo": x["repository"]["name"], "pr": x["number"], "juntado": x["mergedAt"],
                       "categoria": c, "detalhe": d})
    if not a.sem_commits:
        itens += lotes_diretos(a.org, sorted(repos), a)

    serie = collections.defaultdict(collections.Counter)
    for mes, c, _ in itens:
        serie[mes][c] += 1
    unid = collections.Counter((c, u) for _, c, u in itens)
    if a.json:
        json.dump({"regra": "github-profile/tools/contar_entregas.py", "unidades": "pr; lote = commits diretos do mesmo "
                   "repo, dia e categoria; card = lote da central", "serie_mensal": {m: dict(serie[m]) for m in sorted(serie)},
                   "prs": por_pr}, sys.stdout, ensure_ascii=False, indent=1)
        return
    anos = sorted({m[:4] for m in serie})
    total = lambda filtro, c: sum(v[c] for m, v in serie.items() if filtro(m))   # noqa: E731
    print("categoria   " + "".join(f"{x:>8}" for x in anos) + "  início   (PRs / lotes / cards)")
    for c in ORDEM:
        print(f"{c:11} " + "".join(f"{total(lambda m, y=y: m.startswith(y), c):8}" for y in anos)
              + f"{total(lambda m: True, c):8}   ({unid[(c, 'pr')]} / {unid[(c, 'lote')]} / {unid[(c, 'card')]})")
    print("dirigidas   " + "".join(f"{sum(total(lambda m, y=y: m.startswith(y), c) for c in DIRIGIDAS):8}" for y in anos)
          + f"{sum(total(lambda m: True, c) for c in DIRIGIDAS):8}")
    print("\nmês      " + "".join(f"{c[:8]:>9}" for c in ORDEM))
    for m in sorted(serie):
        print(f"{m}  " + "".join(f"{serie[m][c]:9}" for c in ORDEM))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv[1:])
