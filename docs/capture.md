# Capturing a collection

The solver needs two things the game does not export: what you own, and what you
can spend. Both come off screen recordings, and they come off *different*
screens, which is the whole shape of this procedure.

Read this before recording anything long. The single biggest saving here is not
recording faster — it is recording less.

---

## Filter first. This is the whole game.

A full box is 2,900+ Pokémon. At the ~3 seconds each that reliable OCR needs,
that is over two hours of swiping, and almost all of it is wasted: a level 8
Rattata is never going to earn a power-up.

Pokémon GO's own search bar narrows the box before you record anything:

| Search | Keeps |
|---|---|
| `cp2000-` | anything already worth investing in |
| `4*` | perfect and near-perfect IVs |
| `shadow`, `lucky`, `purified` | cheaper or dearer to power up |
| `age0-1` | caught in the last day, for a top-up scan |

Scanning 200 candidates instead of 2,929 turns this from a weekend job into a
coffee break — and it is the reason automating the swipe usually is not worth it.

A filtered pass also **tags itself**. Everything in a `lucky` pass is lucky by
construction, so those flags need no image classification at all. That is why
`scan_session.search_filter` exists.

---

## Pass 1 — the appraisal screen, for IVs

Open a Pokémon, open **Appraise**, and leave it open. It stays open as you swipe,
which is what makes this one pass rather than two taps per Pokémon.

```bash
python ocr_ingest.py --video swipe.mp4 -o scan.csv
```

Gives species, CP, HP, the three IV bars, and — solved from CP and HP — the
level. One pause per **Pokémon**.

**Hold on each one for about a second.** That is not politeness to the OCR, it is
the mechanism: frames are grouped in time and every field is voted across the
group, so a single misread loses the vote instead of becoming a row in your
collection. Sampling defaults to every 5th frame; at 30fps a one-second hold is
six votes.

---

## Pass 2 — the plain detail screen, for candy

Close the appraisal. Swipe again, pausing on each **family** — not each Pokémon.

```bash
python ocr_ingest.py --video candy.mp4 --candy-out candy.csv
```

Gives stardust, candy, and candy XL, all three exact.

Two reasons this is much shorter than pass 1. Candy is pooled per evolution
family, so every Marill, Azumarill and Azurill you own share one `MARILL CANDY`
number. And you do not need all of them — see below.

### Why the appraisal screen cannot do this

The appraisal overlay puts a rating badge over the stardust figure and the team
leader over the candy one. Swiping does move the card out from behind them, and
reading it there works often enough to be tempting — but the candy **icon** sits
immediately left of the digits and gets read as one of them. `MARILL CANDY 1,211`
came back as `41,211`: correctly grouped in thousands, plausible as a count, and
34 times the truth. Nothing downstream can catch that.

On the plain card the icon is orange against dark teal text and is removed by
hue, which is why a row reads `521,865 / 1,645 / 293` exactly. Hence two passes.

`--candy-from-overlay` opts into the unreliable reader. It logs a warning, and
you should check anything it produces.

### You probably need very few of these

Run pass 1, solve, and read the warning:

```
WARNING: plan spends candy from 2 families with no known stock
         (the Magikarp family needs 18, the Skiddo family needs 12)
```

That names exactly which families the plan reaches for. Check those in game and
type them into the CSV — usually a handful, not three hundred.

---

## Automating the swipe on iOS

Worth knowing before you set this up:

- **iOS will not let one app drive another.** No Shortcuts action taps a
  coordinate in another app, and Xcode UI tests cannot drive App Store apps you
  do not own. The only supported route is Apple's own accessibility features.
- **Niantic's terms prohibit automation.** Swiping your own collection to read
  your own data is not cheating in any gameplay sense, but their detection does
  not necessarily draw that line, and the penalty is against your account. Your
  call — just make it knowingly.
- **It does not make the pass shorter**, only unattended. The OCR still needs its
  dwell time. Filtering is what makes it shorter.

### AssistiveTouch recipe

1. **Settings → Accessibility → Touch → AssistiveTouch → On**
2. **Create New Gesture…** — swipe left across the middle of the screen, then
   **Save** as `Next Pokemon`.
3. **Custom Actions → Long Press → Next Pokemon** (or attach it to whichever
   trigger you prefer).

That gives one tap-to-swipe. For hands-off repetition:

4. **Settings → Accessibility → Switch Control → Switches → Add Switch → Screen
   → Full Screen**, action **Select Item**.
5. **Auto Scanning → On**, and set **Auto Scanning Time** to your dwell — about
   **1.5 seconds** pairs with the one-second hold the voting wants.

Start the recording, start Switch Control, and let it run. Check the first
twenty Pokémon before walking away: if the swipe lands on the wrong part of the
screen it will page past several at once, and you will not get that back.

### Sanity check the result, not the setup

```bash
python ocr_ingest.py --video swipe.mp4 -o scan.csv
```

Compare the record count against how many Pokémon you meant to scan. Fewer means
the swipe was outrunning the dwell time — raise the auto-scanning interval.

---

## Then solve

```bash
python run.py --source scan --csv scan.csv --candy candy.csv --stardust 250000
```

Rows flagged in `scan.csv` are worth a look before you act on the plan. The
scanner never drops a Pokémon for a bad read — it flags it — so a low
`iv_confidence` or a `species not named` warning is an invitation to fix one cell
in a spreadsheet, not to re-record.
