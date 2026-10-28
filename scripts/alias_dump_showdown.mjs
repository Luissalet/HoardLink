// Dumps Showdown's names for scripts/build_alias_data.py. __SILVER__ is replaced by the Silver's Hoard folder.
import { createRequire } from 'node:module';
import fs from 'node:fs';
const req = createRequire("__SILVER__/package.json");
const { Dex } = req('@pkmn/sim');
const d = Dex.mod('gen9');
const ok = (x) => x.exists && x.num > 0 && !['CAP', 'Custom'].includes(x.isNonstandard || '');
const data = {
  species: d.species.all().filter(ok).map((s) => ({ id: s.id, name: s.name, num: s.num, base: s.baseSpecies, forme: s.forme || '' })),
  moves: d.moves.all().filter(ok).map((m) => ({ id: m.id, name: m.name })),
  abilities: d.abilities.all().filter(ok).map((a) => ({ id: a.id, name: a.name })),
  items: d.items.all().filter(ok).map((i) => ({ id: i.id, name: i.name })),
  natures: d.natures.all().filter((n) => n.exists).map((n) => ({ id: n.id, name: n.name })),
  types: d.types.all().filter((t) => t.exists).map((t) => ({ id: t.id, name: t.name })),
};
fs.writeFileSync(process.argv[2], JSON.stringify(data));
