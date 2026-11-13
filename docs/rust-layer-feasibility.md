# Réécrire WineGDK, ou Wine, en Rust ? Étude de faisabilité

Étude du 2026-11-13, faite sur la branche `3.0.0` (`f6adbab`, moteur
`wow64-archs-native23`). Elle ne change aucun code. Les chiffres viennent du
dépôt, du WineGDK local (`681dfcd1105`), du jeu installé (1.26.60.5) et des
mesures déjà faites sur la machine de dev (Ryzen 5 5600, RTX 4060). Chaque
chiffre dit d'où il vient.

## Verdict

**Ne réécrivez ni Wine ni WineGDK en Rust.** Aucune des options de réécriture
ne rapporterait de performance, parce que le temps ne passe pas dans cette
couche. Ce qui pèse vraiment, c'est le fork : un arbre Wine 11.1 complet,
compilé à part puis posé sur GE-Proton 10. Le bon investissement est
l'option (d) : **sortir les DLL GDK du fork comme DLL PE autonomes, ce
qu'elles sont déjà, et revenir sur un Proton amont avec un tout petit jeu
de patches.** Rust n'a sa place que plus tard, pour du code neuf et isolé
derrière ces DLL, jamais pour traduire ce qui marche déjà.

| Option | Effort pour une personne | Gain de perf plausible | Verdict |
|---|---|---|---|
| (a) réécrire Wine entier | des décennies-personne | ≈ 0 (voir plus bas) | **Non** |
| (b) `xgameruntime` + DLL GDK en Rust, chargées par un Wine amont | 4 à 8 mois pour revenir à la parité | ≈ 0 | **Non, en tant que réécriture.** Oui pour la moitié « DLL PE hors de l'arbre », qui n'exige pas Rust |
| (c) chargeur PE sur mesure, sans Wine | 2 à 5 ans-personne avant d'atteindre un monde avec compte | quelques % d'un cœur au mieux | **Non** |
| (d) garder le moteur, réduire la dette (rebase Proton 11, upstream) | 3 à 6 semaines | petit mais réel : ce que Proton corrige sans nous | **Oui**, avec le jalon décrit en fin de document |

## 1. Ce qu'est vraiment Cinnabar, et ce qui se transpose

Dépôt `cinnabar-mc/cinnabar`, lu le 2026-11-13 (branche `dev`, v0.2.1 du
2026-11-07, dépôt créé le 2026-07-11).

- **Ce n'est pas une couche de compatibilité.** C'est un client Bedrock
  réécrit de zéro : rendu, monde, UI JSON, physique, inventaires, en Rust
  (Bevy 0.20/wgpu). Le binaire de Mojang n'y entre jamais. Le README le décrit
  comme « an independent, unofficial client », qui « may not yet match the
  official client exactly, so server anticheats may flag you ».
- **Le réseau et l'authentification ne sont pas en Rust.** Ils vivent dans un
  service Go (`bedrock-core`) qui reprend gophertunnel, go-raknet,
  go-nethernet, go-xsapi et go-playfab. Le client Rust lui parle par une
  socket locale. Les mondes solo passent par un BDS, il n'y a pas de serveur
  intégré.
- **La cible est épinglée sur une seule version.** `assets/bedrock-target.json`
  fixe le protocole 2225 (1.26.60) et des registres de blocs et de biomes
  extraits pour cette version. Chaque mise à jour du jeu est un travail de
  portage.
- **Taille et rythme.** Environ 2,06 M lignes de Rust hors codec généré,
  0,12 M de Go, plus de 120 crates, 38 contributeurs, 3 045 PR. Un mainteneur
  signe 6 669 commits en quatre mois, et le dépôt documente un flux
  multi-agents (`docs/agents/`). Le `plan.md` se décrit lui-même comme
  incomplet : rythme d'images, latence d'entrée et parité de comportement
  sont « not fully qualified ».
- **Licence : PolyForm Noncommercial 1.0.0.** Elle est incompatible avec la
  licence MIT de BedrockOnLinux et avec la redistribution libre du moteur
  LGPL. Aucune ligne de Cinnabar ne peut entrer dans ce dépôt.

Ce qui se transpose :

1. **La raison de leurs gains ne se transpose pas.** Cinnabar peut aller plus
   vite que le jeu officiel parce qu'il remplace le moteur de Minecraft lui-même,
   le thread qui construit chaque image. BedrockOnLinux garde ce binaire, donc
   ce levier ne nous est pas accessible. Réécrire la couche du dessous ne touche
   pas au code qui consomme le temps.
2. **La séparation est déjà faite chez nous.** Chez Cinnabar,
   l'authentification et le transport sont dans un processus à part, dans un
   langage choisi pour ses bibliothèques. Ici c'est déjà le cas : `xodus-cli`
   (Rust) et la pré-authentification de `bol/` (Python) le font hors de Wine.
3. **Le rythme montre ce qu'un développeur avec des agents peut produire.**
   Mais c'est du volume dans un domaine testable contre de vrais serveurs, avec
   une seule version cible. Une couche Win32 doit au contraire suivre chaque
   mise à jour d'un binaire fermé.

## 2. La surface dont le jeu dépend vraiment

### 2.1 Ce que le moteur ajoute au-dessus de Wine

Le moteur est `GE-Proton10-32` (Wine 10) sur lequel `scripts/package-engine.sh`
pose le préfixe WineGDK (Wine 11.1, `75637b674e1f`). Ce dernier ajoute
lui-même les r12, native5 et les patches `0002` à `0017`. Dans le WineGDK
local, le delta depuis `Release 11.1` compte 330 commits et 792 fichiers. La
plupart sont des reprises d'amont. La part propre au GDK et au jeu se mesure
par dossier :

| Dossier | Lignes ajoutées depuis 11.1 |
|---|---|
| `dlls/xgameruntime` | 14 807 (≈ 12 000 hors tests) |
| `dlls/windows.storage` + `windows.storage.applicationdata` | 4 704 (sélecteurs WinRT) |
| `dlls/gameinput` | 3 005, **désactivé en jeu** (`gameinput=d`, #281/#359) |
| `dlls/ntdll` + `server` | 2 142 (fsync, image mappée, `/proc`, fuseau horaire) |
| `dlls/twinapi.appcore`, `combase`, `winewayland.drv`, `winex11.drv`, `kernelbase` | ≈ 1 100 |

En tout, environ 26 000 lignes de C. Wine en compte plusieurs millions et
729 DLL dans cet arbre.

Ventilation des 17 patches de `third_party/winegdk-native5/` :

| Patch | Touche | Nature | Réversible vers l'amont ? |
|---|---|---|---|
| 0001, 0003, 0006, 0009, 0012, 0017 | `xgameruntime` | GDK : XUser, Realms, XStore, XSystem, Gaming Services, XGameEvent | non, c'est la partie propre au projet |
| 0001, 0002, 0010 | `windows.storage*` | sélecteurs de fichiers WinAppSDK et WinRT | oui, candidats à l'upstream dans Wine |
| 0004 | `combase` | rétroportage d'`IContextCallback` amont | déjà dans Wine récent |
| 0005 | `winex11.drv` | rétroportage sémantique de `b868cd31d6b` | déjà dans Wine récent |
| 0007, 0008 | `ntdll` (chargeur) | image principale depuis un fd ou un fichier aussitôt délié (exe Store chiffré) | portage de `xodus-gaming/wine` `183d5d90b62a` ; à garder |
| 0011 | `winewayland.drv` | contourne les en-têtes wayland 1.18 de Bullseye | disparaît avec des en-têtes ≥ 1.21 |
| 0013 | `kernelbase` | `PrivateUsage` dans `GetProcessMemoryInfo` (#292) | oui, correction de bogue nette |
| 0014 | `ntdll` + `server` | fsync de Valve (`b62913d8cab`) | **déjà dans Proton 11** |
| 0015, 0016 | `ntdll` | deux appels par image sortis du thread principal | oui, upstream possible |

Ce qui ne peut ni venir de l'amont ni devenir une DLL : `0007`/`0008`, deux
patches de 227 lignes en tout.

### 2.2 `xgameruntime.dll` est déjà une DLL PE autonome

C'est le fait le plus important de l'étude. Imports de la DLL livrée
(`LC_ALL=C x86_64-w64-mingw32-objdump -p`) :

```
advapi32.dll bcrypt.dll combase.dll crypt32.dll kernel32.dll ntdll.dll ucrtbase.dll winhttp.dll
```

Il n'y a aucun export propre à Wine dans ses imports de `ntdll`, et pas de
partie Unix : aucun `xgameruntime.so` dans les mappings du jeu. Elle
n'exporte que dix points d'entrée (`xgameruntime.spec`) :
`QueryApiImpl`, `InitializeApiImpl*`, `XGameRuntimeInitialize`,
`XErrorReport`, `DllGetClassObject`. Tout le reste passe par des vtables COM
servies par `QueryApiImpl`. `main.c` reconnaît XSystem, XGameRuntimeFeature,
XGame, XSystemAnalytics, XThreading, XNetworking, XUser, XStore (composite
`0dd112ac`), XGameEvent (0017) et le contexte XSAPI `834366da`.

En pratique, **l'option (b) est déjà réalisée, en C** : un Wine amont peut
charger ce binaire tel quel. Ce qui l'attache au fork, c'est seulement la
façon dont il est construit, dans l'arbre de Wine, et non son code.

À noter : `xgameruntime.dll.threading` (4,6 Mo, épinglé depuis la base
GDK-Proton dans `package-engine.sh`) est un binaire MSVC, qui importe les
jeux `api-ms-win-*`. Il contient les chemins
`gamecore\grts\runtime\windows\…\xal\…` et des messages XAL : c'est très
probablement le runtime GDK de Microsoft lui-même. `XAsync` lui délègue les
files de tâches natives (patch 0006). Toute réécriture devrait le remplacer
aussi, et sa redistribution mérite une vérification de licence, quelle que
soit l'option retenue.

### 2.3 Ce que le jeu charge réellement

Il n'y a pas de liste d'imports dans le dépôt, et celle de l'exe est
illisible sur disque : les segments `KEEP_ENCRYPTED_ON_DISK` sont chiffrés.
J'ai donc relevé les modules mappés par le jeu 1.26.60.5 en cours de partie
(`/proc/<pid>/maps`, en lecture seule, le 2026-11-13) :

- **68 DLL PE de Wine** : `ntdll`, `kernel32`, `kernelbase`, `user32`,
  `gdi32`, `win32u`, `combase`, `ole32`, `oleaut32`, `rpcrt4`, `winhttp`,
  `wininet`, `schannel`, `crypt32`, `bcrypt`, `secur32`, `kerberos`,
  `ws2_32`, `iphlpapi`, `dnsapi`, `nsi`, `mmdevapi`, `winmm`, `hid`,
  `setupapi`, `cfgmgr32`, `shell32`, `shcore`, `propsys`, `msxml3`, `msxml6`,
  `urlmon`, `gdiplus`, `dbghelp`, `xinput1_4`, `wintypes`, les CRT MSVC et
  d'autres. À cela s'ajoutent `system32` (`d3d12`/`d3d12core`/`dxgi` de
  vkd3d-proton et DXVK, `windows.ui`, `windows.web`,
  `windows.ui.core.textinput`, `twinapi.appcore`,
  `windows.devices.enumeration`, `GameInputRedist` : 12 fichiers) et
  **14 bibliothèques Unix de Wine** (`ntdll.so`, `win32u.so`, `winex11.so`,
  `winevulkan.so`, `winepulse.so`, `ws2_32.so`, `bcrypt.so`, `crypt32.so`,
  `secur32.so`, `kerberos.so`…).
- **33 DLL livrées avec le jeu**, dont `cohtml` + **V8** (Ore UI, avec un JIT
  qui exige mémoire exécutable, SEH et déroulement x64 exacts),
  `RenoirCore`, `fmod` (audio, donc WASAPI), `libHttpClient`, les thunks
  XSAPI, `PlayFabMultiplayerGDK`, le bootstrap WindowsAppRuntime et le jeu
  XCurl/OpenSSL.
- 59 threads. Ce n'est pas un jeu qui ne parlerait qu'à D3D12 et aux
  fichiers : c'est une application WinRT/COM complète, avec un navigateur
  embarqué.

## 3. Où passe le temps aujourd'hui

Toutes les mesures déjà faites pointent hors de WineGDK :

| Poste | Mesure | Source |
|---|---|---|
| **Thread principal du jeu** (code de Mojang) | 85 à 88 % d'un cœur en monde, GPU à 41-45 % : **c'est lui le plafond des FPS** | banc raw VA du 2026-11-12, gamescope headless 1600×900 |
| vkd3d : CBV en raw VA (obligatoire, #27) contre push descriptors | 502-617 FPS contre 778-785 FPS, une seule paire de runs, pas livrable en l'état | même banc |
| vkd3d : `force_raw_va_cbv` sur NVIDIA | jusqu'à ≈ 15 % (commentaire amont de `state.c`) | mémoire `force-raw-va-cbv` |
| vkd3d : limiteur d'images | 20-22 % → 3,8-5,9 % d'un cœur (corrigé dans native23) | `fix-frame-limiter-busy-wait.patch` |
| Synchronisation Wine sans ntsync | wineserver 5 % contre 1,5 %, 200 contre 224 FPS ; fsync 250-255 FPS | patch 0014 |
| Appels Wine sur le thread principal | 2,8 % + 1,9 % → presque rien | patches 0015, 0016 |
| Latence souris → image | 139 → 75 ms en cadençant sous le rafraîchissement : la file d'attente vient de Xwayland, pas de Wine | `docs/INPUT-LATENCY.md` |
| Lancement (17,1 s) | auth Xbox 4,6 s, `xodus-cli` 5,6 s, conteneur 3,1 s, **Wine → fenêtre 1,9 s** | mémoire `launch-time-budget` |
| « Ça lag » sur la machine de dev | swap et RAM, jamais le GPU | mémoire `dev-machine-lag-profile` |

Échantillon pris le 2026-11-13 sur la partie en cours (moteur native23,
ntsync actif, 12 s, état du jeu inconnu), en % d'un cœur :
`vkd3d-swapchain` 14,2, `Rendering_Pool` 7,2 + 3,2, `vkd3d_queue` 3,0,
**`wineserver` 1,7**, `Streaming_Pool` ≈ 6 au total, audio 1,8, thread
principal 0,9. Le total vaut 39 %.

Ce que fait réellement Wine, c'est-à-dire wineserver plus les appels traduits
encore visibles, pèse **moins de 2 % d'un cœur** une fois ntsync ou fsync en
place. Même une couche idéale, d'un coût nul, ne pourrait récupérer que ça.
Les vrais leviers sont dans vkd3d (descripteurs, limiteur), dans le
compositeur (rythme, vsync) et dans le code du jeu, que personne ici ne
réécrit.

## 4. Les options une par une

### (a) Réécrire Wine entier en Rust — non

- **Effort** : Wine, c'est trente ans de travail, des centaines de
  contributeurs et plusieurs millions de lignes. Même réduit aux modules que
  le jeu charge, on retombe sur l'option (c). Ce n'est pas faisable seul,
  agents compris : leur rythme suppose une cible qu'on peut tester. Ici, il
  faudrait reproduire des comportements non documentés, que le jeu découvre à
  chaque mise à jour.
- **Perf** : voir § 3. La part de Wine est déjà sous les 2 %.
- **Licence** : un portage ligne à ligne serait une œuvre dérivée LGPL-2.1+.
  Une réécriture propre pourrait être sous MIT. Cela ne change rien au
  verdict.

### (b) `xgameruntime` et les DLL GDK en Rust, chargées par un Wine amont — non pour Rust, oui pour l'autonomie

- **La moitié utile existe déjà** (§ 2.2). Il suffit de construire
  `xgameruntime.dll` et `windows.storage*.dll` **hors de l'arbre Wine**, avec
  MinGW, comme on le fait déjà pour vkd3d-proton et le jeu XCurl, puis de les
  déposer dans un Proton amont. Effort : 1 à 3 semaines, presque tout dans le
  système de build (en-têtes IDL de Wine, `winecrt0`, canaux de débogage).
- **La réécriture en Rust** (`windows-rs` pour COM/WinRT, cible
  `x86_64-pc-windows-gnu`) : environ 12 000 lignes de C pour `xgameruntime`,
  plus 4 700 pour les sélecteurs, avec les vtables, les layouts et les
  sémantiques `XAsync`/`XTaskQueue` à reproduire exactement. Comptez 4 à 8
  mois pour revenir à la parité. Le gros du temps n'est pas l'écriture : c'est
  la requalification en jeu réel de chaque chaîne déjà déboguée (sign-in #214,
  #366, Realms #266, XStore #171, succès #152, Gaming Services #274), soit
  des dizaines d'entrées de mémoire du projet.
- **Perf** : nulle. `xgameruntime` n'a aucun thread chaud dans le relevé du
  § 2.3, et ses chemins sont réseau ou ponctuels.
- **Ce que Rust apporterait vraiment** : la sûreté mémoire dans le code
  asynchrone et de comptage de références, là où ont eu lieu de vrais bogues
  (use-after-free des sélecteurs, pointeur nul de #171, propriété des files
  de tâches). C'est réel, mais ça ne justifie pas de tout re-qualifier.
- **Risques** : chaque version du jeu peut demander une nouvelle interface
  (1.26.50 : IID `67ce4bfc`, 0009) ou une version de Gaming Services plus
  récente (1.26.70 : 7849, 0012). Le coût de suivi est le même en C et en
  Rust. Sans `xgameruntime.dll.threading`, il faudrait aussi réécrire les
  files de tâches natives.

### (c) Chargeur PE sur mesure sans Wine — non

- **Surface** : les plus de 100 modules du § 2.3. Le plus dur ne se voit pas
  dans une liste d'imports : SEH et déroulement x64 pour MSVC C++ et le JIT
  de V8, TLS, activation COM et WinRT (WinAppSDK, sélecteurs, `windows.ui`),
  WinHTTP + Schannel + WebSockets, DXGI et la chaîne Vulkan sous vkd3d (qui
  passe aujourd'hui par `winevulkan`), la pile HID pour GameInput, WASAPI
  pour fmod, le registre, le chemin d'image chiffrée de Xodus.
- **Précédents** : `loadlibrary` de Tavis Ormandy charge une DLL
  sans interface graphique. Darling vise macOS depuis plus de dix ans et n'est
  toujours pas complet. Il n'existe aucun précédent pour un titre WinRT avec
  navigateur embarqué.
- **Effort** : 2 à 5 ans-personne avant d'atteindre un monde avec compte,
  puis une course sans fin derrière chaque mise à jour.
- **Perf** : au mieux, on retire wineserver (moins de 2 %) et quelques
  traductions d'appels. Le thread principal (85-88 %) et vkd3d ne bougent pas.
- **Anti-triche** : Bedrock n'en a pas côté client sous Windows. Les
  anti-triches de serveur jugent le comportement réseau, que le jeu non
  modifié garde dans tous les cas. Le risque est faible, mais on ne gagne rien
  non plus.

### (d) Garder le moteur, réduire la dette — oui

Aujourd'hui, le moteur dépend d'un fork archivé (`Weather-OS/WineGDK`,
`archived: true`) gelé sur Wine 11.1, et ce fork est posé sur GE-Proton 10.
Chaque correction d'amont doit être rétroportée à la main : 0004 et 0005 en
sont deux, 0014 en est une troisième. Pendant ce temps, Valve et GE publient
Proton 11 (`proton-11.0-2e`, `GE-Proton11-12` du 2026-11-03).

1. **Construire les DLL GDK hors de l'arbre**, avec la même discipline
   reproductible et attestée que vkd3d et XCurl. Elles deviennent un
   composant de plus dans `docs/BUILD.md`, et non plus un fork de Wine.
2. **Prendre Proton 11 comme base**, à la place de GE-Proton 10 + Wine 11.1.
   0014 (fsync) y est déjà, 0004 et 0005 viennent de l'amont, 0011 tombe avec
   des en-têtes wayland récents.
3. **Ne garder en patch que ce qui ne peut pas être ailleurs** : 0007/0008
   (chargeur), plus temporairement 0013, 0015 et 0016.
4. **Proposer en amont** 0013, 0015, 0016 et les sélecteurs (0002, 0010) à
   Wine. 0013 corrige un bogue Windows évident et ne touche à rien de
   spécifique au jeu.

- **Effort** : 3 à 6 semaines, dont l'essentiel en CI et en requalification.
- **Perf** : petite, mais elle s'accumule : ce que Proton corrige (Wayland,
  synchronisation, vkd3d intégré, wow64) arrive sans rétroportage.
- **Maintenance** : la dette passe d'un arbre Wine de 330 commits à une
  poignée de patches courts et une DLL qu'on contrôle.
- **Risques** : Proton 11 peut casser un comportement dont le jeu dépend.
  C'est exactement ce que le jalon ci-dessous mesure avant tout engagement.

## 5. Premier jalon : un moteur témoin sur Proton 11

Le but est de savoir en une semaine combien des 17 patches survivent
réellement sur Proton 11, avant d'écrire la moindre ligne.

1. Reconstruire `GE-Proton11-12`, ou `proton-11.0-2e`, avec seulement
   `0007` et `0008` : sans eux, l'exe chiffré ne se charge pas.
2. Y déposer, sans rien recompiler, les fichiers de l'engine native23 :
   `xgameruntime.dll`, `xgameruntime.dll.threading`,
   `windows.storage.dll` et `windows.storage.applicationdata.dll`. Utiliser
   un préfixe neuf, puis enregistrer `FileSavePicker` comme le fait déjà le
   lanceur.
3. Lancer 1.26.60.5 avec le banc existant (gamescope headless, verrou de banc,
   jamais pendant que le joueur joue). Cocher : menu, sign-in, Social, Realms,
   un monde, un import (sélecteur), un export (sauvegarde), un succès. Mesurer
   les FPS en monde et le CPU du wineserver contre native23.
4. Pour chaque case qui échoue, appliquer le patch native correspondant, et
   lui seul.

**Résultat attendu** : un tableau de huit cases cochées, avec les patches
nécessaires sur Proton 11 en regard. Si le jeu va jusqu'au monde avec compte
avec 0007/0008 et les DLL déposées, le fork WineGDK n'a plus de raison
d'exister, et la suite de (d) devient du travail de CI. Si ça casse, la liste
des cases rouges chiffre exactement le coût, ce qu'aucune réécriture ne
dirait.

Rust ne devient pertinent qu'après ce jalon, pour une pièce neuve et isolée,
par exemple remplacer `xgameruntime.dll.threading` le jour où sa licence ou
son comportement l'imposerait. Le C qui marche reste en C.
