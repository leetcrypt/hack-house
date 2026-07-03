#!/usr/bin/env bash
# Batch-harvest a genre-diverse character roster for the hh card-art library.
# Covers the card types the live VM registry actually uses (Normal/Psychic/
# Flying/Steel/Ghost/Electric/Fire) with MULTIPLE distinct faces per type so
# cards stop sharing a portrait. Mixes Pokémon, Digimon and Yu-Gi-Oh.
set -u
cd "$(dirname "$0")/.."
PY=python3

# NAME | QUERY | ELEMENTS | TIER | KEYWORDS | CREDIT
ROSTER=$(cat <<'EOF'
Snorlax|Snorlax pokemon official artwork|Normal|Uncommon|heavy sleepy tank bulk immovable|Pokemon
Eevee|Eevee pokemon official artwork|Normal|Common|adaptable versatile cute evolving|Pokemon
Ditto|Ditto pokemon official artwork|Normal|Common|shapeshifter clone transform copy mimic|Pokemon
Regigigas|Regigigas pokemon official artwork|Normal|Legendary|titan colossus ancient golem legendary|Pokemon
Agumon|Agumon digimon render|Normal Fire|Common|rookie brave dinosaur partner plucky|Digimon
Yugi|Yugi Muto Yu-Gi-Oh character art|Normal|Rare|duelist strategist heart cards hero|Yu-Gi-Oh
Mew|Mew pokemon official artwork|Psychic|Legendary|mythical origin playful ancestor rare|Pokemon
Alakazam|Alakazam pokemon official artwork|Psychic|Epic|intellect genius spoons brain calculate|Pokemon
Espeon|Espeon pokemon official artwork|Psychic|Rare|foresight elegant sun intuition graceful|Pokemon
DarkMagician|Dark Magician Yu-Gi-Oh artwork|Psychic Ghost|Epic|spellcaster arcane sorcerer magic conjure|Yu-Gi-Oh
Rayquaza|Rayquaza pokemon official artwork|Flying Dragon|Legendary|sky serpent ozone legendary skybound|Pokemon
Pidgeot|Pidgeot pokemon official artwork|Flying Normal|Rare|swift raptor scout recon aerial|Pokemon
Lugia|Lugia pokemon official artwork|Flying Psychic|Legendary|guardian sea storm diving legendary|Pokemon
BlueEyes|Blue-Eyes White Dragon Yu-Gi-Oh artwork|Flying Dragon|Legendary|destruction white legendary burst power|Yu-Gi-Oh
Metagross|Metagross pokemon official artwork|Steel Psychic|Epic|supercomputer armored quad calculating fortress|Pokemon
Aggron|Aggron pokemon official artwork|Steel Rock|Rare|iron fortress defense armored tank|Pokemon
MetalGreymon|MetalGreymon digimon render|Steel|Epic|cyborg missiles armored machine upgraded|Digimon
Dusknoir|Dusknoir pokemon official artwork|Ghost|Epic|reaper spectral netherworld grim sentinel|Pokemon
Chandelure|Chandelure pokemon official artwork|Ghost Fire|Rare|spectral flame haunting lantern eerie|Pokemon
Zapdos|Zapdos pokemon official artwork|Electric Flying|Legendary|thunder storm legendary charged bolt|Pokemon
Luxray|Luxray pokemon official artwork|Electric|Rare|vision predator feline hunt piercing|Pokemon
Blaziken|Blaziken pokemon official artwork|Fire|Epic|blazing martial striker kick fierce|Pokemon
Greymon|Greymon digimon render|Fire|Rare|dinosaur nova blaze horn champion|Digimon
EOF
)

n=0
while IFS='|' read -r NAME QUERY ELEMENTS TIER KEYWORDS CREDIT; do
  [ -z "$NAME" ] && continue
  n=$((n+1))
  echo "== [$n] $NAME ($ELEMENTS / $TIER) =="
  $PY scripts/imgharvest.py "$QUERY" \
      --name "$NAME" --element $ELEMENTS --tier "$TIER" \
      --keywords $KEYWORDS --credit "$CREDIT" --max 3 --candidates 18 \
    || echo "  ! $NAME failed"
done <<< "$ROSTER"
echo "ALL DONE ($n characters)"
