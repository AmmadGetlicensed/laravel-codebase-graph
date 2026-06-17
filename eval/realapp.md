# Running the eval against a real Laravel app

The tiny fixture proves LaravelGraph returns correct *structural* facts (routes,
models, events, dead code). It **cannot** prove the differentiated value — live
database ground truth, N+1 detection, model↔table linking — because it has no
running database. To measure that, point the harness at a real Laravel app.

## 1. Choose an app and configure a read-only DB connection

```bash
export LARAVELGRAPH_EVAL_REAL_APP=/path/to/your/laravel/app
```

Add a **read-only** database connection so phases 24–26 (live introspection,
model–table linking, DB access analysis) can run. In
`$LARAVELGRAPH_EVAL_REAL_APP/.laravelgraph/config.json`:

```json
{
  "databases": [
    {
      "name": "primary",
      "driver": "mysql",
      "host": "127.0.0.1",
      "port": 3306,
      "database": "your_db",
      "username": "readonly_user",
      "password": "..."
    }
  ]
}
```

Use a least-privilege, read-only DB user. The harness only issues `SELECT` /
`information_schema` reads, but defense in depth matters.

## 2. Author ground-truth questions

Edit `eval/dataset/real.yaml`. Each question needs facts you *know* are true —
real column names, the model that backs a table, a method you know contains an
N+1. Lean into questions a file-reading agent would get wrong:

- "What columns does `X` table have in the live DB?" (vs. what migrations say)
- "Which model maps to table `Y`?"
- "What is the full event→listener→job chain when `Z` happens?"
- "Where are the N+1 risks in the `W` code path?"

## 3. Run

```bash
# Deterministic structural correctness (no API key)
python -m eval.run_eval --mode structural --app real

# A/B: agent accuracy WITH vs WITHOUT LaravelGraph (needs ANTHROPIC_API_KEY)
python -m eval.run_eval --mode agent --app real
```

Scorecards are written to `eval/results/`. The headline number is **lift**
(`accuracy_with − accuracy_without`). If lift is large on DB-intelligence /
N+1 questions but near zero on generic routes/models, that confirms the
strategic call: lead with the DB and static-analysis moat.

## Notes

- Indexing a large app takes longer than the tiny fixture; embeddings are
  disabled by the harness regardless.
- The agent A/B run makes real API calls (one agent loop WITH + one WITHOUT +
  two judge calls per question). Cost scales with question count.
