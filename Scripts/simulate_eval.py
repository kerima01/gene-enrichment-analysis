#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simulate_eval.py
Expérimentations : comparaison des mesures (binomial, hypergeometric, chi2, coverage)

@author: kerima issa
"""

import random
from pathlib import Path
import pandas as pd
import neo4j
from scipy.stats import binom, hypergeom, chi2_contingency
import time

# PARAMÈTRES EXPÉRIMENTAUX

TARGET_LABEL = "Keyword"           # label Neo4j pour les sets cibles
REPS = 2                         # répétitions par configuration
Q_SIZES = [10, 25, 50]             # tailles des requêtes
SIGNALS = [0.25, 0.5, 0.75]        # fraction de "signal" (éléments tirés dans la vraie cible)
MEASURES = ["binomial", "hypergeometric", "chi2", "coverage"]

TMP = Path("tmp2")
TMP.mkdir(exist_ok=True)

# Neo4j connection 
NEO4J_URI = "neo4j://localhost"
NEO4J_USER = "neo4j"
NEO4J_PASS = "omybioinfo"

# FONCTIONS DE MESURE

def score_binomial(c, q, t, N):
    """p-value binomial P(X >= c) with p = t/N and n = q"""
    if q <= 0:
        return 1.0
    p = t / N if N > 0 else 0.0
    # binom.sf(k-1, n, p) gives P(X >= k)
    return float(binom.sf(c-1, q, p))

def score_hypergeom(c, q, t, N):
    """p-value hypergeometric P(X >= c)"""
    if q <= 0 or N <= 0:
        return 1.0
    return float(hypergeom.sf(c-1, N, t, q))

def score_chi2(c, q, t, N):
    """Chi2 test on 2x2 contingency table; return p-value."""
    a = c
    b = q - c
    d = t - c
    e = N - q - t + c
    # ensure ints
    # if any expected cell is negative -> invalid -> return 1.0
    if any(x < 0 for x in (a, b, d, e)):
        return 1.0
    try:
        chi2, p, dof, expected = chi2_contingency([[a, b], [d, e]])
        return float(p)
    except Exception:
        return 1.0

def score_coverage(c, q, t, N):
    """Coverage distance as defined in the assignment:
       cov = 1 - (c/q * c/t)  (lower = better, 0 when identical, 1 when no overlap)
       We return this score (so sorting ascending keeps best first).
    """
    if q == 0 or t == 0:
        return 1.0
    return 1.0 - ((c / q) * (c / t))

# mapping
MEASURE_FUN = {
    "binomial": score_binomial,
    "hypergeometric": score_hypergeom,
    "chi2": score_chi2,
    "coverage": score_coverage
}

# For measures that are p-values (binomial, hypergeom, chi2) lower = better.
# For coverage lower = better too. So sorting ascending works for all.


# RÉCUPÉRATION DES SETS DE RÉFÉRENCE DEPUIS NEO4J

print(">>> Connexion à Neo4j pour charger les sets cibles...")
drv = neo4j.GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
cy = f"""
MATCH (t:{TARGET_LABEL})-[]->(g:Gene)
RETURN t.id AS id, collect(g.id) AS members
"""
df_sets = drv.execute_query(cy, result_transformer_=neo4j.Result.to_df)
drv.close()

# Data cleaning: df may have duplicates or None rows
df_sets = df_sets.dropna(subset=["id"]).drop_duplicates(subset=["id"])
sets_dict = {row.id: set(row.members) for _, row in df_sets.iterrows()}
if len(sets_dict) == 0:
    raise SystemExit("Aucune cible trouvée pour le label demandé. Vérifie le label Neo4j (TARGET_LABEL).")

population = set().union(*sets_dict.values())
N_population = len(population)
print(f"Nombre de cibles : {len(sets_dict)}")
print(f"Taille population : {N_population}")

# EXPÉRIMENTATIONS

results = []
start_time = time.time()
total_tasks = len(sets_dict) * len(Q_SIZES) * len(SIGNALS) * REPS * len(MEASURES)
task_count = 0

# Iterate through each target to generate Q sets based on that target (as in original design)
for t_id, members in sets_dict.items():
    t = set(members)
    if len(t) < 5:
        continue  # skip very small targets

    for qsize in Q_SIZES:
        for p_signal in SIGNALS:
            for rep in range(REPS):
                c = max(1, int(round(p_signal * qsize)))  # number of signal elements
                if len(t) < c:
                    continue
                # sample signal from true target
                signal = set(random.sample(list(t), c))
                noise_pool = list(population - t)
                if len(noise_pool) < qsize - c:
                    continue
                noise = set(random.sample(noise_pool, qsize - c))
                Q = signal.union(noise)
                qlen = len(Q)

                # compute measure scores for ALL targets (ranking)
                scores = []
                for other_id, other_members in sets_dict.items():
                    T = other_members
                    tlen = len(T)
                    c_common = len(Q.intersection(T))
                    fn = MEASURE_FUN  # local alias
                    # compute score
                    try:
                        score = MEASURE_FUN["binomial"]  # dummy to satisfy linter
                    except Exception:
                        pass

                    # call correct function
                    score_val = MEASURE_FUN["binomial"](c_common, qlen, tlen, N_population)
                    # adjust to correct measure later in loop
                    scores.append((other_id, c_common, tlen, score_val))

                # Now compute for each measure separately (so we can reuse counts)
                for measure in MEASURES:
                    # build a list (target_id, value) for sorting
                    list_values = []
                    for (other_id, c_common, tlen, _dummy) in scores:
                        # compute score with appropriate function
                        val = MEASURE_FUN[measure](c_common, qlen, tlen, N_population)
                        # for p-values we keep val as-is (lower better). For coverage the function already returns lower=better.
                        list_values.append((other_id, val, c_common, tlen))

                    # sort ascending (lower = better)
                    list_values.sort(key=lambda x: (x[1], -x[2]))  # tie-breaker: more overlap (higher c) better

                    # find rank of true target t_id
                    rank = None
                    for idx, (other_id, val, c_common, tlen) in enumerate(list_values, start=1):
                        if other_id == t_id:
                            rank = idx
                            # capture top value too
                            true_val = val
                            break

                    results.append({
                        "target": t_id,
                        "size": qsize,
                        "signal": p_signal,
                        "rep": rep,
                        "measure": measure,
                        "rank": rank,
                        "score": true_val if rank is not None else None,
                        "qsize_actual": qlen,
                        "target_size": len(t)
                    })

                    task_count += 1
                # progress display occasionally
                if task_count % 500 == 0:
                    elapsed = time.time() - start_time
                    print(f"Progress: {task_count}/{total_tasks} tasks, elapsed {elapsed:.1f}s")

# ANALYSE DES RÉSULTATS

df = pd.DataFrame(results)

# Save full detailed results
df.to_csv("tmp2/results_all.csv", index=False, sep="\t")

def MRR(ranks):
    # ranks contains None for missing -> treat as 0 contribution
    inv = [(1.0 / r) if (r and r > 0) else 0.0 for r in ranks]
    return float(pd.Series(inv).mean())

# group and summarize
summary = (
    df.groupby(["measure", "size", "signal"])
      .agg(
          p1 = ("rank", lambda r: float((r == 1).mean())),
          p5 = ("rank", lambda r: float((r <= 5).mean())),
          mrr = ("rank", lambda r: MRR(r)),
          median_rank=("rank", lambda r: float(pd.Series([x for x in r if x is not None]).median()) if any(r.notnull()) else None),
          valid=("rank", "count")
      )
      .reset_index()
)

summary.to_csv("tmp2/summary.tsv", sep="\t", index=False)

elapsed_total = time.time() - start_time
print("\n Résultats enregistrés : summary.tsv (résumé) et results_all.csv (détaillé)")
print(f"Temps total : {elapsed_total:.1f}s")
print(c_common)
print(summary)
