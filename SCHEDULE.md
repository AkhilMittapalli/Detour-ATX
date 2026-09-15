# Running it every morning

The daily job is one command:

```
python cli.py --run-all
```

It builds a brief for every route in `routes/`, writes HTML to `out/`, and
records what each reader was told so tomorrow suppresses the repeats.

## Delivery

**Every transport is a dry run unless you pass `--send`.** Without it the run
reports exactly what it would have delivered and delivers nothing.

```
python cli.py --run-all                         # writes to out/, sends nothing
python cli.py --run-all --transport email       # dry run: shows what it would email
python cli.py --run-all --transport email --send
python cli.py --run-all --transport webhook --send
```

Email needs six values in `.env` &mdash; see `.env.example`. Use an **app
password**, never your account password. A webhook needs only
`DETOUR_WEBHOOK_URL`, and must be HTTPS.

### Silence is not a message

A brief with nothing new is **not sent**. The Correspondent already suppresses
repeats, so most mornings have nothing to say, and a daily "nothing to report"
email is its own kind of spam. After seven quiet days it sends one all-clear
so you know the job is still running.

## Windows Task Scheduler

```
schtasks /create /tn "Detour ATX" /tr "python D:\Portfolio\detour-atx\cli.py --run-all" /sc daily /st 06:45
```

## cron

```
45 6 * * *  cd /path/to/detour-atx && python cli.py --run-all
```

## Before scheduling

Get a Socrata app token and put it in `.env` as `SOCRATA_APP_TOKEN`. It is
free, and without one every run shares a throttled pool with everyone else
hitting the portal anonymously.

## What it costs per run

One description rewrite per *new* item only — the Correspondent holds back
repeats before they reach the renderer, so a steady-state morning with nothing
new makes no model calls at all. The Verifier and Desk Editor are separate
commands and are not part of the daily job.
