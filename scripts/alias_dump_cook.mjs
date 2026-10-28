// Dumps CookHoard's ingredient dictionary for scripts/build_alias_data.py. __COOK__ is replaced by the CookHoard folder.
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
const { BUILTIN_INGREDIENTS } = await import(pathToFileURL("__COOK__/packages/core/src/dictionary.ts").href);
fs.writeFileSync(process.argv[2], JSON.stringify(BUILTIN_INGREDIENTS.map((i) => ({
  id: i.id, name: i.name, nameEn: i.nameEn, aliases: i.aliases, category: i.category }))));
