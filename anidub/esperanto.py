PHONETICS_TABLE = [
    {
        "char": "Ĉ / ĉ",
        "name": "ĉo",
        "pronunciation": "ch",
        "exampleWord": "Ĉokolado",
        "exampleMeaning": "Chocolate",
        "pronundescription": "Sounds like 'ch' in 'chair' or 'church'.",
    },
    {
        "char": "Ĝ / ĝ",
        "name": "ĝo",
        "pronunciation": "j",
        "exampleWord": "Ĝardeno",
        "exampleMeaning": "Garden",
        "pronundescription": "Sounds like 'j' in 'joy' or 'gem'.",
    },
    {
        "char": "Ĥ / ĥ",
        "name": "ĥo",
        "pronunciation": "kh",
        "exampleWord": "Ĥoruso",
        "exampleMeaning": "Chorus / Choir",
        "pronundescription": "A raspy, German 'ch' or Scottish 'ch' in 'loch'.",
    },
    {
        "char": "Ĵ / ĵ",
        "name": "ĵo",
        "pronunciation": "zh",
        "exampleWord": "Ĵurnalo",
        "exampleMeaning": "Newspaper",
        "pronundescription": "Sounds like 's' in 'measure' or 'g' in 'garage'.",
    },
    {
        "char": "Ŝ / ŝ",
        "name": "ŝo",
        "pronunciation": "sh",
        "exampleWord": "Ŝuo",
        "exampleMeaning": "Shoe",
        "pronundescription": "Sounds like 'sh' in 'show' or 'shine'.",
    },
    {
        "char": "Ŭ / ŭ",
        "name": "ŭo",
        "pronunciation": "w",
        "exampleWord": "Antaŭ",
        "exampleMeaning": "Before / Front",
        "pronundescription": "Sounds like 'w' in 'cow' or 'now'. Used as a semi-vowel.",
    },
]


def build_instruct_prompt(speaker_name: str | None = None) -> str:
    table_rows = "\n".join(
        f'  {{\n'
        f'    char: "{row["char"]}",\n'
        f'    name: "{row["name"]}",\n'
        f'    pronunciation: "{row["pronunciation"]}",\n'
        f'    exampleWord: "{row["exampleWord"]}",\n'
        f'    exampleMeaning: "{row["exampleMeaning"]}",\n'
        f'    pronundescription: "{row["pronundescription"]}"\n'
        f'  }}'
        + ("," if i < len(PHONETICS_TABLE) - 1 else "")
        for i, row in enumerate(PHONETICS_TABLE)
    )
    table_json = f"[\n{table_rows}\n]"

    header = (
        "Speak with perfect Esperanto phonetics, natural rhythm and clear diction:\n"
        "Speak the following Esperanto text with perfect, natural Esperanto "
        "pronunciation, smooth word links, and correct phonetics: "
    )

    speaker_hint = ""
    if speaker_name:
        speaker_hint = (
            f'\n\nSpeak as the character "{speaker_name}". '
            "Use a youthful, feminine voice with a slightly tsundere edge."
        )

    return f"{header}\n{table_json}{speaker_hint}"