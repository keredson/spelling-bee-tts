import re
from pathlib import Path
import pronouncing
import pandas as pd
import numpy as np

in_path = Path("NGSLwithSFI-31K.xlsx")
df = pd.read_excel(in_path)

# ----------------------------
# Syllable counting
# ----------------------------
def estimate_syllables(word: str) -> int:
    """Heuristic syllable estimator for OOV words."""
    w = re.sub(r"[^a-z]", "", str(word).lower())
    if not w:
        return 0
    # vowel groups
    groups = re.findall(r"[aeiouy]+", w)
    syll = len(groups)

    # common silent endings
    if w.endswith("e") and not w.endswith(("le", "ye")):
        syll -= 1

    # -le ending (table, little) counts as a syllable when preceded by consonant
    if w.endswith("le") and len(w) > 2 and w[-3] not in "aeiouy":
        syll += 1

    # ensure at least 1
    return max(1, syll)

# Try CMUdict via pronouncing; fallback to heuristic.
def syllable_count(word: str) -> int:
    w = str(word).lower()
    phones = pronouncing.phones_for_word(w)
    if phones:
        return pronouncing.syllable_count(phones[0])
    return estimate_syllables(w)

df["Syllables"] = df["Lemma"].apply(syllable_count).astype(int)

# ----------------------------
# Irregular spelling flag (heuristic)
# ----------------------------
# A small high-impact irregular set + pattern heuristics for likely silent/irregular letter sequences.
IRREGULAR_WORDS = {
    # very common irregulars / tricky
    "the","of","to","one","once","two","does","done","gone","said","says","are","were","was","what",
    "who","whom","whose","where","there","their","they","have","give","live","love","some","come",
    "because","could","would","should","through","though","thought","enough","tough","bought","caught",
    "laugh","laughed","people","friend","again","against","busy","business","pretty","answer","whole",
    "hour","honest","write","wrote","written","right","knight","know","knew","knife","knock",
    "listen","often","island","colonel","choir","queue"
}

# Patterns that often indicate silent letters or uncommon mappings; meant as a *flag*, not a rule.
IRREGEX = re.compile(
    r"""
    (^kn)|(^wr)|(^ps)|(^pn)|(^pt)|(^gn)|(^rh) |   # silent initial consonants
    (mb$)|(mn$)|(bt$)|(gn$) |                     # silent final consonants
    (ough)|(eigh)|(tion)|(sion)|(cial)|(tch)|(gue$)|(que$)|(eau)|(igh) |
    (gh($|t))|(ph)|(ch$)|(dge)|(tion$)|(sion$)|(sure$)|(cious$)|(tious$) |
    (ae)|(oe)|(ei)|(ui)|(ou)|(ow)|(aw)|(au)       # vowel teams (sometimes irregular)
    """,
    re.IGNORECASE | re.VERBOSE
)

def irregular_flag(word: str) -> int:
    w = re.sub(r"[^a-z]", "", str(word).lower())
    if not w:
        return 0
    if w in IRREGULAR_WORDS:
        return 1
    # very short CVC-type words are rarely "irregular" in a spelling-teaching sense
    if len(w) <= 3 and re.fullmatch(r"[bcdfghjklmnpqrstvwxyz]?[aeiou][bcdfghjklmnpqrstvwxyz]", w):
        return 0
    return 1 if IRREGEX.search(w) else 0

df["Irregular_Spelling_Flag"] = df["Lemma"].apply(irregular_flag).astype(int)

# ----------------------------
# Difficulty calculation (uses SFI)
# ----------------------------
def normalize(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    mn, mx = s.min(), s.max()
    if pd.isna(mn) or pd.isna(mx) or mx == mn:
        return s.fillna(0) * 0
    return (s.fillna(mn) - mn) / (mx - mn)

def zscore(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    mean = s.mean()
    std = s.std()
    if pd.isna(mean) or pd.isna(std) or std == 0:
        return s.fillna(0) * 0
    return (s.fillna(mean) - mean) / std

def logistic(series: pd.Series) -> pd.Series:
    return 1.0 / (1.0 + np.exp((-series).clip(-12, 12)))

# SFI is already log-scaled; use z-score + logistic to avoid min-max stretching.
sfi_hard = 1 - logistic(zscore(df["SFI"]))

syll_norm = normalize(df["Syllables"])

# Dispersion: lower dispersion tends to be harder (more domain-clustered)
d_norm = normalize(df["D"])
disp_hard = 1 - d_norm

# Coverage: lower coverage = harder
cov_norm = normalize(df["Coverage"])
cov_hard = 1 - cov_norm

# Irregular: binary penalty (will be normalized implicitly by weighting)
irr = df["Irregular_Spelling_Flag"].astype(float)

# Weights: tilt more toward syllables so longer words rank harder more often.
df["Difficulty"] = (
    0.25 * sfi_hard +
    0.40 * syll_norm +
    0.20 * irr +          # binary, acts like a constant bump
    0.10 * cov_hard +
    0.05 * disp_hard
)

# Rescale to 1–10 for UI
df["Difficulty_1_10"] = 1 + 9 * normalize(df["Difficulty"])

# Save
out_path = Path("words.csv.gz")
out_path_plain = Path("words.csv")
df_out = df[["Lemma", "Difficulty", "Coverage", "Cumulative Coverage"]].rename(
    columns={
        "Lemma": "word", 
        "Difficulty": "difficulty",
        "Coverage": "coverage",
        "Cumulative Coverage": "cumulative_coverage",
    }
)
df_out = df_out.sort_values("difficulty")
df_out.to_csv(out_path, index=False)
df_out.to_csv(out_path_plain, index=False)


print('wrote:', out_path, out_path_plain)
