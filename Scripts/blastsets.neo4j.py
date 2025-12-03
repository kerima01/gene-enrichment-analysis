#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Oct  7 16:42:13 2025

@author: kerima issa
"""
import neo4j
import argparse
from os.path import isfile
import json
from scipy.stats import binom, hypergeom
import pandas as pd


# SCRIPT PARAMETERS
parser = argparse.ArgumentParser(description='Search enriched terms/categories in the provided (gene) set')
parser.add_argument('-q', '--query', required=True, help='Query set.')
parser.add_argument('-t', '--sets', required=True, help='Target sets filename.')
parser.add_argument('-a', '--alpha', required=False, type=float, default=0.05, help='Significance threshold.')
parser.add_argument('-c', '--adjust', required=False, action="store_true", help='Adjust for multiple testing (FDR).')
parser.add_argument('-m', '--measure', required=False, default='binomial', help='Dissimilarity index: binomial (default), hypergeometric, chi2 or coverage.')
parser.add_argument('-l', '--limit', required=False, type=int, default=0, help='Maximum number of results to report.')
parser.add_argument('-v', '--verbose', required=False, action="store_true", help='Talk a lot.')
param = parser.parse_args()

drv = neo4j.GraphDatabase.driver("neo4j://localhost", auth=("neo4j", "omybioinfo"))
drv.verify_connectivity()

# LOAD QUERY
text = param.query
query = set()
if isfile(text):
    with open(text) as f:
        content = ' '.join(f.read().split('\n')).split()
        query |= set(content)
else: # parse string
    query |= set(text.split())

#if param.verbose:
#  print(f'query set: {query}')
  
cypher = "MATCH (n:Keyword) RETURN count(n) AS nb_Keyword"
records, summary, keys = drv.execute_query(cypher)

#if param.verbose:
#   print("param:", param)

cypher = "MATCH (n:Gene) RETURN count(n) AS nb_Gene"
records, summary, keys = drv.execute_query(cypher)
population_size= records[0]['nb_Gene']
#if param.verbose:
#   print("param:", param)



cypher = f"MATCH (t:{param.sets})-[]->(g:Gene) WHERE g.id IN {list(query)} RETURN DISTINCT t.id AS id, t.name AS name"
#print("cypher relevant sets", cypher)

sets = drv.execute_query(cypher, result_transformer_=neo4j.Result.to_df)

#sets = list(df.id)
#if param.verbose:
#    print("target sets:", sets)

# EVALUATE SETS
results = []
query_size = len(query)

for s in sets.itertuples():
    cypher = f"MATCH (t:{param.sets})-[]->(g:Gene) WHERE t.id='{s.id}' RETURN DISTINCT g.id AS id"
    df = drv.execute_query(cypher, result_transformer_=neo4j.Result.to_df)
    elements = set(df.id)
    if len(elements) < 2:
        continue
#    if param.verbose:              
#        print(f"set {s}, elements: {elements}")
    common_elements = elements.intersection( query )
    if param.measure=='binomial': # par défaut binom.cdf(<=success, attempts, proba). il nous faut calculer p(X>=x)
        pvalue = binom.sf(len(common_elements)-1, query_size, len(elements)/population_size)
    elif param.measure == 'hypergeometric':
        # Loi hypergéométrique : P(X >= c)
        c = len(common_elements)
        q = query_size
        t = len(elements)
        g = population_size
        pvalue = hypergeom.sf(c-1, g, t, q)

    elif param.measure == 'coverage':
        # Coverage = 1 - (c/q * c/t)
        c = len(common_elements)
        q = query_size
        t = len(elements)
        if q == 0 or t == 0:
            pvalue = 1.0
        else:
            coverage_score = 1 - ((c / q) * (c / t))
            # On stocke ce score dans pvalue (car le script trie par p-value)
            pvalue = coverage_score

    elif param.measure == 'chi2':
        # Test du chi² d'indépendance
        from scipy.stats import chi2_contingency

        c = len(common_elements)
        q = query_size
        t = len(elements)
        g = population_size

        # Table de contingence :
        #          T        G\T
        # Q        c       q-c
        # G\Q     t-c     g-q-t+c
        a = c
        b = q - c
        d = t - c
        e = g - q - t + c

        table = [[a, b], [d, e]]

        try:
            chi2, pvalue, dof, expected = chi2_contingency(table)
        except:
            # Si une case = 0 → chi2 peut planter pour ça on a decider de renvoie p=1
            pvalue = 1.0

    else:
        print(f"Sorry {param.measure} not yet implemented")
        exit(1)

    results.append({'id': s.id, 'desc': s.name, 'common.n': len(common_elements), 'target.n': len(elements), 'p-value': pvalue, 'genes': ', '.join(common_elements)})

#if param.verbose:
#    print(results)

res_df = pd.DataFrame(results).sort_values('p-value')
if param.adjust:
    m = len(res_df)
    res_df['p-adjust'] = res_df['p-value'] * m / (res_df.reset_index().index + 1)
    res_df['p-adjust'] = res_df['p-adjust'].clip(upper=1.0)
    res_df = res_df[res_df['p-adjust'] < param.alpha]
else:
    res_df = res_df[res_df['p-value'] < param.alpha]

if param.limit > 0:
    res_df = res_df.head(param.limit)

if len(res_df) == 0:
    print("No significant enrichment found.")
else:
    print(res_df.to_csv(sep="\t", index=False))
drv.close()
