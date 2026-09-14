# led-data-acquisition

Script para baixar, direto do Portal de Dados Abertos do TSE, as propostas de
governo (PDF) de todos os candidatos a governador (27 UFs) e a presidente,
nas eleições de 2026.

## Fontes de dados

| Dataset | URL | Uso |
|---|---|---|
| Proposta de governo | `proposta_governo_2026_<UF>.zip` (um ZIP por UF, `BR` para presidente) | Contém os PDFs de proposta de governo |
| Cadastro de candidatos | `consulta_cand_2026.zip` | Usado só para filtrar quais PDFs pertencem a um candidato a `GOVERNADOR`/`PRESIDENTE` |

Ambos hospedados em `cdn.tse.jus.br`. Dataset confirmado em
[dadosabertos.tse.jus.br/dataset/candidatos-2026](https://dadosabertos.tse.jus.br/dataset/candidatos-2026).

## Por que o cadastro de candidatos é necessário

Cada PDF dentro do ZIP de uma UF é nomeado como:

```
<ANO><UF><SQ_CANDIDATO>_<NN>.pdf
ex: 2026PE170002540337_01.pdf
```

O `SQ_CANDIDATO` é um ID interno do TSE, o nome do arquivo não traz o nome
do candidato nem o cargo. Para saber a quem cada PDF pertence (e se é
realmente um candidato a governador/presidente), é preciso cruzar esse ID
com o cadastro nacional de candidatos.

**Achado durante a validação:** dentro do ZIP de uma UF, o TSE às vezes
publica o *mesmo* PDF de proposta associado a mais de um `SQ_CANDIDATO`. Em
Pernambuco (2026), por exemplo, das 8 chapas a governador, só uma teve o PDF
duplicado também sob o `SQ_CANDIDATO` do vice-governador (as outras 7 não).
Ou seja, **não existe uma regra geral e confiável de "todo vice duplica"**;
foi tratado como uma anomalia pontual daquela candidatura específica.

Por isso o filtro implementado é **positivo**, não uma exclusão por cargo: o
script mantém apenas os PDFs cujo `SQ_CANDIDATO` está registrado como
`GOVERNADOR` ou `PRESIDENTE` no cadastro nacional (`load_valid_candidate_ids`
+ `extract_matching_pdfs`, em `baixar_propostas_governo.py`). Qualquer outro
arquivo, vice, o `leiame.pdf` (glossário do TSE), ou qualquer outra
duplicata, é descartado automaticamente, sem depender de conhecer o motivo
exato de cada caso.

## Candidatos sem proposta publicada

Rodando pra todas as UFs, sobraram 2 de 213 candidatos sem PDF nenhum no ZIP
do TSE: GAROTINHO (RJ) e POLICIAL EDJANE (SP). Não é bug do script, são
casos em que o TSE ainda não tinha nenhuma proposta de governo publicada pra
esse candidato até o momento do download. Pode ser que apareça se rodar de
novo mais perto da eleição.

Vale registrar também: o ZIP do RJ veio com um PDF de proposta de uma
candidata a deputado estadual junto (não governador), e o filtro por
SQ_CANDIDATO/cargo descartou certo. Várias candidaturas mandaram a proposta partida em mais de um PDF (`_01`,
`_02`, etc) em vez de um arquivo único; o script trata isso
sem problema, já que todos os arquivos batem no mesmo `SQ_CANDIDATO`.

## Requisitos

```bash
pip install requests
```

## Uso

```bash
# todos os estados + presidente
python baixar_propostas_governo.py

# só alguns (útil para testar antes de rodar tudo)
python baixar_propostas_governo.py --ufs PE,BR

# força novo download mesmo se o arquivo já existir localmente
python baixar_propostas_governo.py --force
```

## Saída

```
tse_propostas_2026/
├── _zips_brutos/          # ZIPs originais baixados do TSE (cache, não rebaixa se já existir)
├── _extraido/<UF>/        # PDFs de proposta de governo já filtrados
└── execucao.log           # log completo da execução
```

## Boa convivência com o servidor público

- `User-Agent` identifica o projeto (`led-data-acquisition/1.0`) nas
  requisições, em vez do padrão genérico da biblioteca `requests`. Facilita
  a vida de quem olhar os logs de acesso do TSE.
- Delay de 1,5s entre downloads (`DOWNLOAD_DELAY_SECONDS`). Evita rajadas de
  requisições contra um servidor público usado por outros
  pesquisadores/jornalistas.
- Retry automático com backoff exponencial (2s, 4s, 8s, 16s, 32s) em erros
  `429`/`5xx`, para tolerar instabilidades momentâneas do servidor sem
  precisar reiniciar o download manualmente.
- Downloads são cacheados localmente (pulados se já existirem), então rodar
  o script de novo não rebaixa o que já foi obtido, a menos que `--force`
  seja usado.
