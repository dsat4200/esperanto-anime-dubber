VOWEL_RULES = """Esperanto has exactly 5 pure vowels. Each vowel is ALWAYS pronounced
as one syllable — vowels NEVER blend into diphthongs like in English:

  a — always 'ah' as in 'father' (never 'ay' or 'uh')
  e — always 'eh' as in 'bet' or 'dress' (never 'ee')
  i — always 'ee' as in 'machine' or 'see' (never 'eye' or 'ih')
  o — always 'oh' as in 'go' or 'note' (never 'oo' or 'aw')
  u — always 'oo' as in 'boot' or 'rule' (never 'yoo' or 'uh')

VOWEL BLENDS ARE ALWAYS SEPARATE SYLLABLES. Each vowel gets its own beat:
  'tie' = 'tee-eh' (2 syllables, never like English 'tie')
  'via' = 'vee-ah' (2 syllables, never like English 'via')
  'kiel' = 'kee-ehl' (2 syllables, never like 'keel')
  'scias' = 'stsee-ahs' (2 syllables)
  'krei' = 'kreh-ee' (2 syllables)
  'plua' = 'ploo-ah' (2 syllables)
  'morgaŭ' = 'mohr-gah-oo' (3 syllables, ŭ = short 'oo')
  'eĥo' = 'eh-khoh' (2 syllables)

CONSONANT BLENDS WITH 'J':
  'aj' = 'eye' (as in English 'eye' or 'my')
  'ej' = 'ay' (as in English 'day' or 'say')
  'oj' = 'oy' (as in English 'boy' or 'toy')
  'uj' = 'ooy' (like 'we' but ending in 'y', or 'ui' in French 'oui')
  'ajn' = 'eye-n' (like 'mine' but ending in 'n', 1 syllable)
  'ejo' = 'ay-oh' (2 syllables: 'ay' + 'oh')

SEMI-VOWEL Ŭ:
  'aŭ' = 'ow' as in English 'cow' or 'now'
  'eŭ' = 'eh-oo' blended quickly (like Spanish/Italian 'Europa')"""


PHONETICS_TABLE = [
    {
        "char": "Ĉ / ĉ",
        "name": "ĉo",
        "pronunciation": "ch",
        "exampleWord": "Ĉokolado",
        "exampleMeaning": "choh-koh-LAH-doh",
        "pronundescription": "Always 'ch' as in 'chair' or 'church'.",
    },
    {
        "char": "Ĝ / ĝ",
        "name": "ĝo",
        "pronunciation": "j",
        "exampleWord": "Ĝardeno",
        "exampleMeaning": "jahr-DEH-noh",
        "pronundescription": "Always 'j' as in 'joy' or 'gem'. Never hard 'g'.",
    },
    {
        "char": "Ĥ / ĥ",
        "name": "ĥo",
        "pronunciation": "kh",
        "exampleWord": "Ĥoruso",
        "exampleMeaning": "khoh-ROO-soh",
        "pronundescription": "Raspy guttural 'ch' like Scottish 'loch' or German 'Bach'.",
    },
    {
        "char": "Ĵ / ĵ",
        "name": "ĵo",
        "pronunciation": "zh",
        "exampleWord": "Ĵurnalo",
        "exampleMeaning": "zhoor-NAH-loh",
        "pronundescription": "Like 's' in 'measure' or French 'j' in 'Jean'.",
    },
    {
        "char": "Ŝ / ŝ",
        "name": "ŝo",
        "pronunciation": "sh",
        "exampleWord": "Ŝuo",
        "exampleMeaning": "SHOO-oh",
        "pronundescription": "Always 'sh' as in 'show' or 'shine'.",
    },
    {
        "char": "Ŭ / ŭ",
        "name": "ŭo",
        "pronunciation": "w",
        "exampleWord": "Antaŭ",
        "exampleMeaning": "AHN-tow",
        "pronundescription": "Short 'oo' blend like 'w' in 'cow'. Only appears in 'aŭ' and 'eŭ'.",
    },
]


def build_instruct_prompt(speaker_name: str | None = None) -> str:
    table_rows = "\n".join(
        f'  {{'
        f' char: "{row["char"]}",'
        f' pronunciation: "{row["pronunciation"]}",'
        f' example: "{row["exampleWord"]}" ({row["exampleMeaning"]}),'
        f' description: "{row["pronundescription"]}"'
        f' }}'
        + ("," if i < len(PHONETICS_TABLE) - 1 else "")
        for i, row in enumerate(PHONETICS_TABLE)
    )
    table_json = f"[\n{table_rows}\n]"

    speaker_hint = ""
    if speaker_name:
        speaker_hint = (
            f'\n\nSpeak as the character "{speaker_name}". '
            "Match their vocal tone, age, personality, and emotional delivery style."
        )

    return (
        "You are an Esperanto voice actor. Follow these rules exactly:\n\n"
        + VOWEL_RULES
        + "\n\nSpecial Esperanto letters:\n"
        + table_json
        + "\n\nIMPORTANT: Every letter is pronounced. No silent letters. "
        "Stress ALWAYS falls on the second-to-last syllable (penultimate). "
        "Speak with natural rhythm, clear diction, and smooth word linking."
        + speaker_hint
    )