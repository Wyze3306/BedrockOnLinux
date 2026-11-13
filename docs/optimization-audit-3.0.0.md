# Audit d'optimisation, BedrockOnLinux 3.0.0

Audit en lecture seule de la branche `3.0.0` (commit `f6adbab`, moteur
`wow64-archs-native23`, xodus `64d39eb87a56-p14`).

**Méthode et limites.** Rien n'a été lancé : ni le jeu, ni les tests, ni un
profileur. Tous les gains ci-dessous sont des **estimations non mesurées**,
déduites de la lecture du code et de quelques tailles de fichiers relevées
sur l'installation de la machine de dev (`~/.local/share/bedrock-on-linux`).
Chaque piste devra être mesurée avant d'être livrée, avec les outils qui
existent déjà (`tools/measure-frames.sh`, `tools/wine-perf-report.py`,
banc gamescope). Les numéros de ligne sont ceux de `f6adbab`.

Lecture non exhaustive : `bol/gui.py`, `bol/mods.py`, `bol/catalog.py` et le
script `proton` du moteur n'ont été lus que par sondage. Les patches moteur
ont été lus par leurs en-têtes, pas ligne à ligne.

## Les dix meilleures pistes

| # | Piste | Gain estimé (non mesuré) | Effort | Risque |
|---|---|---|---|---|
| 1 | Q1 : fermer le memfd de l'exe déchiffré après la copie | 320 à 420 Mo de RAM rendus pendant toute la session | très faible | faible |
| 2 | M1 : stripper le moteur et retirer wine-mono | archive 863 Mo vers 350 à 450 Mo ; moteur 2,5 Go vers 1,2 à 1,5 Go ; préfixe 0,5 à 0,8 Go de moins | moyen | faible à moyen |
| 3 | Q2 : cache de bytecode Python (aucun format n'en a) | 0,3 à 1 s à chaque ouverture du lanceur et à chaque PLAY | très faible | faible |
| 4 | Q3 : ne préparer le moteur qu'une fois par PLAY | 0,3 à 1 s par PLAY (plus sur disque froid) | faible | faible |
| 5 | Q4 : le reaper ne balaie plus `/proc` toutes les 2 s en jeu | 0,5 à 1,5 % d'un cœur, et moins de prises de `mmap_lock` du jeu | faible | faible |
| 6 | M2 : pré-auth Xbox en éventail (3 étapes au lieu de 8) | 2 à 3 s sur un PLAY à cache froid | moyen | moyen |
| 7 | Q5 : verdict du journal noyau mis en cache par boot | 0,1 à 1 s par PLAY | faible | faible (sécurité GPU : à relire) |
| 8 | M3 : xodus écrit l'image directement dans le dossier de staging | 0,2 à 0,5 s par PLAY et un pic de RAM de 320 à 420 Mo évité | moyen | moyen |
| 9 | P1 : démarrer le conteneur pendant le déchiffrement | 1,5 à 2,5 s par PLAY | élevé | élevé |
| 10 | M4 : mises à jour moteur en delta, archive en xz ou zstd | 600 à 800 Mo de téléchargement évités par révision moteur | moyen à élevé | moyen |

## 1. Gains rapides

### Q1. Le memfd de l'exécutable déchiffré reste ouvert toute la session

- **Où** : `bol/xodus.py:1718-1767` (gabarit `_WRAPPER`, fonction `stage` et
  `os.execvp` final).
- **Aujourd'hui** : `xodus-cli run` déchiffre `Minecraft.Windows.exe` dans un
  memfd sans `FD_CLOEXEC`, puis exécute le wrapper. Le wrapper copie le memfd
  dans un fichier du dossier de staging (`/dev/shm` ou `$XDG_RUNTIME_DIR`),
  réécrit `WINE_DLL_FILE_MAP`, puis fait `execvp` de `umu-run`. Le descripteur
  d'origine n'est jamais fermé : `umu-run` (un processus Python qui vit toute
  la session) en hérite, et le memfd reste donc en mémoire en plus de la
  copie que Wine mappe. L'exe pèse 324 Mo en 1.26.60.5 et 423 Mo en Preview
  1.26.70.24 (tailles relevées sur disque).
- **Aggravant** : avec swapguard, tout ce petit monde est dans un scope
  `MemorySwapMax=0` (`bol/swapguard.py:71`), donc ces pages mortes ne peuvent
  même pas partir en swap.
- **Changement** : `os.close(fd_number)` après chaque copie réussie dans la
  boucle de `stage`, avant l'`execvp`.
- **Gain estimé** : 320 à 420 Mo de RAM rendus pendant tout le jeu. Sur la
  machine de dev, où le lag vient de la RAM et du swap, c'est la piste la
  plus rentable de l'audit.
- **Risque** : faible. À confirmer d'abord par
  `ls -l /proc/<pid umu-run>/fd` pendant une session (lecture seule) : c'est
  une déduction de lecture, pas une observation.
- **Effort** : une ligne et un test du gabarit.

### Q2. Aucun format de paquet ne garde de bytecode Python

- **Où** : `scripts/build-appimage.sh:249-250` et `540-542`,
  `scripts/build-deb.sh:41`, `scripts/build-rpm.sh:62`,
  `flatpak/io.github.wyze3306.BedrockOnLinux.yml:157-158`, lanceur
  `bedrock-on-linux:1-21`.
- **Aujourd'hui** : chaque script supprime `__pycache__` et les `.pyc`, pour
  la reproductibilité. L'AppImage supprime aussi ceux de la bibliothèque
  standard et de PySide6. Or les arbres installés sont en lecture seule
  (`/usr/lib`, squashfs, `/app`) : Python ne peut pas y réécrire de cache, et
  `PYTHONPYCACHEPREFIX` n'est posé nulle part. Le commentaire de
  `build-appimage.sh:536` dit que « le runtime peut recréer le bytecode dans
  son cache utilisateur », ce que rien ne fait. Résultat : les 39 000 lignes
  de `bol/` (et, en AppImage, toute la stdlib importée) sont recompilées à
  chaque ouverture du lanceur, à chaque `bol play`, et à chaque exécution du
  wrapper xodus (qui importe `bol.fixups`, donc `bol/__init__.py`).
- **Changement** : poser `sys.pycache_prefix` sur
  `$XDG_CACHE_HOME/bedrock-on-linux/pycache` dans le script
  `bedrock-on-linux` avant d'importer `bol`, et dans le gabarit du wrapper.
  Aucun `.pyc` n'entre dans les artefacts, la reproductibilité est intacte.
  Le `.pyz` reste à part (zipimport n'utilise pas ce cache).
- **Gain estimé** : 0,3 à 1 s à chaque démarrage du lanceur, davantage en
  AppImage ; quelques dixièmes sur chaque PLAY.
- **Risque** : faible (cache invalide après une mise à jour : Python le
  revalide par mtime et taille de la source).
- **Effort** : très faible.

### Q3. Un PLAY depuis la fenêtre prépare tout deux fois

- **Où** : `bol/gui.py:1316-1330` (`do_setup` puis `launch`),
  `bol/gamesetup.py:115-149`, `bol/launch.py:906-918` et `1066-1078`,
  `bol/winegdk.py:359-386`, `bol/vkd3d.py:392` et `496`, `bol/vkd3d.py:619`.
- **Aujourd'hui** : `do_setup` appelle `ensure_login_deps`, `ensure_winegdk`,
  `ensure_umu`, `boot_prefix`, `install_gameinput`. Puis `launch` rappelle
  `ensure_winegdk`, `boot_prefix`, `install_gameinput`, `ensure_login_deps`
  et `ensure_umu`, et ajoute `prepare_universal_vkd3d`. Chaque
  `ensure_winegdk` et chaque `activate_vkd3d_variant` refait
  `validate_engine_manifest`, qui calcule le SHA-256 de tous les
  `critical_files` et des huit DLL vkd3d : trois validations complètes du
  manifeste par PLAY. S'y ajoutent `patch_proton` (lecture et analyse PE de
  `combase.dll` et `ntdll.dll`, deux à trois fois), le SHA-256 de `user32`
  des deux côtés (`bol/prefix.py:632`), celui de `umu-run`
  (`bol/prefix.py:55`) et de `cryptbase`.
- **Changement** : un mémo de validation, valable pour le processus, indexé
  par la signature `stat` (taille, mtime_ns, inode) de `engine-manifest.json`
  et des fichiers critiques ; ou plus simplement faire porter le résultat de
  `do_setup` à `launch`. La validation à l'installation et au changement de
  révision reste complète.
- **Gain estimé** : 0,3 à 1 s par PLAY à chaud, plusieurs secondes sur un
  disque dur froid.
- **Risque** : faible si la signature `stat` est vérifiée à chaque fois ;
  moyen si on se contente d'un drapeau en mémoire (une DLL modifiée entre
  les deux passes ne serait plus vue).
- **Effort** : faible.

### Q4. Le reaper lit l'environnement de tous les processus toutes les 2 s

- **Où** : `bol/leftovers.py:35` et `71-78`, `bol/prefix.py:562-574`,
  `bol/launch.py:1423-1432`.
- **Aujourd'hui** : pendant toute la partie, `LeftoverReaper.tick` appelle
  `prefix_processes`, qui lit `/proc/<pid>/environ` de chaque processus de
  la machine. Lire l'`environ` d'un processus prend son `mmap_lock` en
  lecture, celui du jeu compris, toutes les deux secondes.
- **Changement** : une fois `minecraft.windows.exe` vu, ne plus surveiller
  que ce PID (existence de `/proc/<pid>`, ou un pidfd) et ne reprendre le
  balayage complet qu'après sa disparition, ce qui est le seul moment où le
  reaper agit.
- **Gain estimé** : 0,5 à 1,5 % d'un cœur selon le nombre de processus, et
  la fin d'une source de contention périodique sur le thread principal.
  Petit, mais gratuit et exactement sur le goulot connu.
- **Risque** : faible (réutilisation de PID : comparer le starttime, comme
  le fait déjà `gpu_safety._process_start`).
- **Effort** : faible.

### Q5. `journalctl` interrogé deux fois à chaque PLAY

- **Où** : `bol/gpu_safety.py:892-939` (`_kernel_journal_text`,
  `_kernel_driver_fault_scope`), appelé par `require_safe_graphics_session`
  depuis `bol/launch.py:1055`.
- **Aujourd'hui** : chaque PLAY lance `journalctl -k -b 0 -n 5000`, puis
  `journalctl -k -b -1 -n 5000` tant que le boot précédent n'a pas été
  acquitté, puis `xrandr --listproviders`, en série et avant tout le reste.
- **Changement** : le journal du boot précédent ne change plus ; mettre son
  verdict en cache, indexé par le `boot_id` courant. Lancer la lecture du
  boot courant dans un thread dès l'entrée de `_launch_once`, en parallèle
  de `_prepare_launch_engine`, et n'attendre son résultat qu'à l'endroit
  actuel.
- **Gain estimé** : 0,1 à 1 s par PLAY (les gros journaux persistants sont
  lents à ouvrir).
- **Risque** : faible, mais c'est le garde-fou GPU : le verdict « current »
  ne doit jamais être mis en cache.
- **Effort** : faible.

### Q6. Six écritures de registre hors ligne, six balayages de `/proc`

- **Où** : `bol/wine_registry.py:275-346`, appels dans `bol/auth.py:1481` et
  `1585`, `bol/hidraw.py:213`, `216` et `235`, `bol/gameinput.py:267`,
  `bol/launch.py:516`.
- **Aujourd'hui** : chaque appel prend le verrou, refait
  `require_prefix_idle` (balayage complet de `/proc`), relit et réanalyse
  `system.reg` (4 Mo) ou `user.reg`.
- **Changement** : accumuler les changements du lancement dans une seule
  transaction, appliquée une fois juste avant `Popen`.
- **Gain estimé** : 0,1 à 0,3 s par PLAY.
- **Risque** : faible ; l'ordre des écritures et l'atomicité existent déjà.
- **Effort** : faible à moyen (toucher cinq modules).

### Q7. Les patchs du dossier du jeu sont revérifiés à chaque lancement

- **Où** : `bol/launch.py:1138-1145` et `1172`, `bol/fixups.py:215-322` et
  `652`, `bol/branding.py:76-446`.
- **Aujourd'hui** : chaque PLAY relit tous les bundles HBUI `index-*.js`,
  rouvre `ui.brarchive`, `splashes.json`, et repasse les expressions
  régulières, pour conclure « déjà appliqué ».
- **Changement** : un tampon par dossier de jeu (version du jeu de patchs,
  taille et mtime des fichiers visés) qui court-circuite la relecture.
- **Gain estimé** : 0,05 à 0,3 s par PLAY.
- **Risque** : faible.
- **Effort** : faible.

### Q8. La sonde swapguard crée un scope systemd à chaque PLAY

- **Où** : `bol/swapguard.py:77-101`.
- **Aujourd'hui** : un `systemd-run --user --scope` jetable, en série, pour
  vérifier que `memory.swap.max` vaut 0.
- **Changement** : mettre le résultat en cache par boot (la délégation du
  contrôleur mémoire ne change pas en cours de session), ou lancer la sonde
  dans le même thread d'avance que Q5.
- **Gain estimé** : 0,05 à 0,2 s par PLAY.
- **Risque** : faible.
- **Effort** : très faible.

## 2. Chantiers moyens

### M1. Le moteur est livré avec ses symboles de débogage, et avec wine-mono

- **Où** : `scripts/build-winegdk-container.sh:294-296`,
  `scripts/build-winegdk-bullseye.sh:335-339`,
  `scripts/package-engine.sh:1105-1107`.
- **Aujourd'hui** : Wine est configuré sans `CFLAGS` ni `CROSSCFLAGS`, donc
  avec le `-g -O2` par défaut, et rien n'est strippé avant l'archive.
  Relevé sur le moteur installé : `shell32.dll` fait 15,5 Mo pour 0,58 Mo de
  `.text`, le reste étant surtout des sections `.debug_*` ; `ntdll.so` est
  « with debug_info ». Le moteur fait 2,5 Go décompressé
  (`x86_64-windows` 813 Mo, `i386-windows` 669 Mo, `share/wine/mono` 257 Mo,
  `share/wine/gecko` 208 Mo) et l'archive 863 Mo.
- **Changement** : stripper les modules PE et ELF au packaging (strip
  déterministe, donc compatible avec la reproduction bit à bit) et publier
  les symboles dans une archive `-debug` séparée, à côté. Retirer wine-mono,
  qu'aucun des titres pris en charge n'utilise. Garder gecko : la connexion
  de Minecraft Education passe par mshtml.
- **Gain estimé** : archive vers 350 à 450 Mo, moteur vers 1,2 à 1,5 Go,
  et 0,5 à 0,8 Go de moins dans `drive_c/windows` de chaque préfixe (les DLL
  y sont copiées telles quelles ; 1,6 Go relevé). Téléchargement, extraction
  et hachages plus rapides d'autant.
- **Risque** : faible à moyen. Les traces `winedbg` perdent leurs noms sans
  l'archive `-debug` ; `tools/wine-perf-report.py` lit `.pdata` en mémoire
  et ne dépend pas du DWARF. Nouvelle révision moteur, donc deux passes CI
  et de nouveaux pins.
- **Effort** : moyen.

### M2. La chaîne de pré-auth Xbox est entièrement séquentielle

- **Où** : `bol/auth.py:1116-1133` (`_xbl_post`) et `1136-1379`.
- **Aujourd'hui** : quand le cache n'est plus réutilisable, huit requêtes
  HTTPS partent l'une après l'autre (device, user, XSTS achievements, puis
  profile, playfab, multiplayer, realms, licensing), chacune sur une
  connexion TLS neuve via `urllib`. Le mémo du projet indique 4,6 s. La
  réutilisation du cache (`_PREAUTH_REUSE_MARGIN`, `bol/auth.py:594`) et le
  préchauffage par la fenêtre sont déjà en place ; l'éventail, lui, n'a
  jamais été commité.
- **Changement** : trois étapes au lieu de huit. `device` et `user` en
  parallèle ; puis XSTS achievements et les cinq `_authorize` en parallèle
  dans un `ThreadPoolExecutor` ; le repli SISU vers XSTS (#149) reste par
  audience.
- **Gain estimé** : 2 à 3 s sur un PLAY à cache froid (premier lancement
  après plus de quelques heures, ou `bol play` sans fenêtre). Nul quand le
  cache est chaud.
- **Risque** : moyen. Xbox Live peut limiter les rafales, et le diagnostic
  par étape (`_record_xbl_preauth_diagnostic`) doit rester déterministe.
- **Effort** : moyen.

### M3. L'image déchiffrée est écrite deux fois en RAM

- **Où** : `bol/xodus.py:1718-1745`, patch xodus à ajouter dans
  `third_party/xodus/patches/`.
- **Aujourd'hui** : xodus déchiffre dans un memfd, puis un wrapper Python
  recopie 324 à 423 Mo vers tmpfs, parce que le conteneur ne peut pas hériter
  d'un numéro de descripteur.
- **Changement** : une option `xodus-cli run --stage-dir <dir>` qui écrit
  directement le fichier 0600 dans le dossier de staging et publie son
  chemin (le patch moteur 0008 accepte déjà un chemin). Le relèvement de la
  réserve de pile se fait sur ce fichier. Le wrapper Python devient un
  simple `exec`, ou disparaît.
- **Gain estimé** : 0,2 à 0,5 s par PLAY (copie, démarrage d'un
  interpréteur, import de `bol`) et un pic de RAM de la taille de l'exe en
  moins. Rend Q1 caduc.
- **Risque** : moyen : patch Rust, nouveau rev `-p15`, deux passes CI.
- **Effort** : moyen.

### M4. Une révision moteur se retélécharge en entier, en gzip

- **Où** : `bol/winegdk.py:277-356`, `scripts/package-engine.sh:1105-1107`,
  `bol/util.py:296-352`.
- **Aujourd'hui** : passer de native22 à native23 change quelques DLL et
  coûte 863 Mo. L'archive est un `tar | gzip -6`, téléchargée en un seul
  flux.
- **Changement**, par ordre d'effort : (a) compresser en xz, que `tarfile`
  lit partout, ou en zstd avec repli ; (b) publier avec chaque révision un
  paquet delta (les seuls fichiers changés depuis la précédente, plus le
  nouveau manifeste), appliqué sur une copie de l'arbre installé puis validé
  par `validate_engine_manifest` exactement comme une archive complète.
- **Gain estimé** : (a) 25 à 35 % sur l'archive ; (b) 600 à 800 Mo évités
  par mise à jour moteur pour les installations existantes. Se cumule avec
  M1.
- **Risque** : moyen. Le manifeste ne couvre aujourd'hui que les fichiers
  critiques : un delta sûr demande un manifeste de tous les fichiers. xz est
  plus lent à décompresser que gzip (une fois par révision).
- **Effort** : (a) faible à moyen, (b) moyen à élevé.

### M5. La fenêtre charge tout `bol` avant d'apparaître

- **Où** : `bol/gui.py:35-65` (dont `49-50`), `bol/cli.py:9-40`.
- **Aujourd'hui** : `gui.py` importe au niveau module `launch` (donc `auth`,
  `xodus`, `mods`, `prefix`, `vkd3d`…), `gui_content` et `gui_mods`, alors
  que les pages Mods et Contenu ne sont construites qu'à leur première
  ouverture (`bol/gui.py:1445`).
- **Changement** : importer `ModsPage`, `ContentPage`, `betterrtx`,
  `relocation` et `launch` là où ils servent. Attention : `bol/__init__.py`
  désactive volontairement les imports paresseux de Python 3.15 (#356) ;
  il s'agit ici d'imports locaux explicites, pas de PEP 810.
- **Gain estimé** : 0,1 à 0,3 s sur l'apparition de la fenêtre une fois Q2
  en place, davantage sans.
- **Risque** : faible.
- **Effort** : faible à moyen.

### M6. Le lanceur reste entier en mémoire pendant la partie

- **Où** : `bol/gui.py:1283-1286` (`window_action_for_launch`),
  `bol/launch.py:1423-1457`.
- **Aujourd'hui** : par défaut la fenêtre reste (« stay »), avec Qt, ses
  pixmaps et les pages déjà construites. Sur une machine à court de RAM,
  c'est de la mémoire que le jeu n'a pas.
- **Changement** : pendant la partie, libérer ce qui se reconstruit
  (pixmaps des éditions, vues du changelog, pages Mods et Contenu) et
  suspendre les minuteries ; ou, pour qui choisit « fermer au lancement »,
  ne garder qu'un superviseur sans Qt.
- **Gain estimé** : 50 à 150 Mo de RAM. Très incertain : à mesurer avant
  d'y toucher.
- **Risque** : moyen (cycle de vie des QThread, déjà source de plantages).
- **Effort** : moyen.

### M7. Régulateur CPU et priorités : rien n'est demandé au système

- **Où** : `bol/launch.py:1146-1308` (construction de la commande et de
  l'environnement) ; aucune occurrence de gamemode, d'affinité ou de
  priorité dans `bol/`.
- **Aujourd'hui** : le jeu tourne avec le régulateur et les priorités du
  bureau. Le goulot connu est le thread principal.
- **Changement** : quand `gamemoded` est présent, lui enregistrer le PID du
  jeu par D-Bus depuis le lanceur (pas de `LD_PRELOAD` dans le conteneur),
  derrière un réglage. Cela règle le régulateur et l'ioprio sans privilège.
- **Gain estimé** : nul sur un fixe déjà en `performance` ou `schedutil`
  réactif ; 5 à 20 % sur un portable en `powersave`. Estimation fragile.
- **Risque** : faible (optionnel, absent du Flatpak sans portail).
- **Effort** : moyen.

## 3. Paris

### P1. Démarrer le conteneur pendant que xodus déchiffre

- **Où** : `bol/launch.py:1273-1349`, `bol/xodus.py:1788-1869`.
- **Aujourd'hui** : la chaîne est strictement en série : `xodus-cli run`
  (licence et déchiffrement, 2,4 s d'après le changelog), puis le wrapper,
  puis `umu-run` et pressure-vessel (3,1 s), puis Wine.
- **Pari** : lancer `umu-run` tout de suite avec un petit programme
  d'attente dans le conteneur, qui n'exécute Wine qu'une fois l'image
  déposée dans le dossier de staging par xodus, lancé en parallèle.
- **Gain estimé** : 1,5 à 2,5 s par PLAY.
- **Risque** : élevé. Un refus de licence doit toujours arrêter le
  lancement avant toute fenêtre et avant le marqueur GPU ; gamescope doit
  rester le plus extérieur ; il faut un nettoyage si l'une des deux moitiés
  échoue.
- **Effort** : élevé. Dépend de M3.

### P2. Alléger ce que le script `proton` refait à chaque lancement

- **Où** : script `proton` du moteur (assemblé par
  `scripts/package-engine.sh`), non lu en détail.
- **Pari** : une partie des 3,1 s « conteneur » est la préparation du
  préfixe par Proton à chaque exécution (fichiers suivis, liens des DLL,
  polices). Un tampon « préfixe déjà à jour pour cette révision » pourrait
  en sauter l'essentiel.
- **Gain estimé** : 0,3 à 1 s par PLAY, sans aucune certitude tant que ce
  n'est pas profilé.
- **Risque** : élevé (c'est ce code qui garde le préfixe cohérent après un
  changement de moteur).
- **Effort** : moyen à élevé.

### P3. Grandes pages transparentes pour le tas du jeu

- **Où** : nouveau patch dans `third_party/winegdk-native5/` (ntdll,
  `virtual.c`).
- **Pari** : la plupart des distributions sont en THP `madvise`, donc Wine
  n'en reçoit aucune. Un `madvise(MADV_HUGEPAGE)` sur les grandes régions
  engagées réduirait les défauts de TLB du thread principal.
- **Gain estimé** : 0 à 10 % sur le thread principal.
- **Risque** : élevé sur la machine même où le problème est la RAM : les
  grandes pages gonflent la mémoire résidente. À n'activer que par variable
  d'environnement, et à mesurer d'abord.
- **Effort** : moyen.

### P4. Options de compilation du moteur

- **Où** : `scripts/build-winegdk-container.sh:294`,
  `scripts/build-vkd3d-universal.sh:223-229`.
- **Pari** : Wine est compilé en `-O2` générique et vkd3d-proton en
  `--buildtype release` générique. Essayer `-march=x86-64-v2 -mtune=generic`
  (ou les options de Proton) sur Wine, et LTO sur vkd3d-proton seulement.
- **Gain estimé** : 0 à 3 %. Les patches 0015 et 0016 ont montré que le
  coût de Wine par image tient à quelques appels précis, pas au code généré.
- **Risque** : moyen (plancher CPU relevé, reproductibilité). LTO et PGO sur
  Wine lui-même sont écartés : non pris en charge proprement pour les
  modules PE.
- **Effort** : moyen. Rapport gain sur effort faible : en dernier.

### P5. Topologie CPU présentée au jeu

- **Où** : `bol/launch.py:753-815` (`_configure_runtime_compat`).
- **Pari** : sur les processeurs hybrides, Bedrock dimensionne ses threads
  de travail sur tous les cœurs logiques ; restreindre aux cœurs
  performants par `WINE_CPU_TOPOLOGY` pourrait stabiliser les temps d'image.
- **Gain estimé** : inconnu, peut être négatif.
- **Risque** : moyen. **Effort** : faible à essayer, élevé à généraliser.

### P6. Doublon possible : exe chiffré et cache du paquet

- **Où** : `bol/xodus.py:854` (`PACKAGE_CACHE`), `944-975`.
- **Constat** : chaque build garde `Minecraft.Windows.exe` chiffré (324 Mo)
  et `.xodus-streaming.msixvc` (401 Mo). Si `run` déchiffre depuis le cache
  du paquet, le fichier chiffré à côté ne sert qu'à porter un nom.
- **Pari** : le remplacer par un fichier creux ou un lien. 300 à 420 Mo par
  build installé.
- **Risque** : élevé tant que le comportement de `xodus-cli streaming`
  (comparaison des hachages de segments pour le delta) n'est pas vérifié
  dans son code.
- **Effort** : moyen.

### P7. Se passer des modules 32 bits et de WebView2

- **Constat** : `i386-windows` pèse 669 Mo dans le moteur et
  `Program Files (x86)` 927 Mo dans le préfixe (WebView2 et son updater,
  que le reaper doit déjà tuer).
- **Pari** : le jeu est 64 bits ; empêcher l'installation de WebView2 et
  livrer un moteur sans i386.
- **Risque** : très élevé (écrans du jeu qui s'appuient sur WebView2,
  Education, Dungeons, Legends).
- **Effort** : élevé. À ne regarder qu'après M1.

## 4. Déjà fait, réfuté ou décidé : à ne pas reproposer

- **ntsync, et repli fsync sans ntsync** : patch 0014, en native23.
- **Limiteur vkd3d sans attente active** : `fix-frame-limiter-busy-wait.patch`.
- **Limiteur du jeu mis de côté, vsync qui tient la limite, plafond au
  rafraîchissement, cadence sous Xwayland pour la latence** :
  `bol/launch.py:304-530`, `docs/INPUT-LATENCY.md`.
- **Swapguard** (`MemorySwapMax=0`, seuil de RAM) : `bol/swapguard.py`.
  `MemoryLow` a été jugé inutile.
- **`force_raw_va_cbv`** : obligatoire (gel du menu sans lui), ne pas le
  retirer malgré son coût sur NVIDIA.
- **Raw VA à une seule root signature** : gain mesuré en monde mais jugé non
  livrable ; ne pas le reproposer sans élément nouveau.
- **`GetProcessMemoryInfo` et fuseau horaire par image** : patches 0015 et
  0016.
- **Locales du Steam Linux Runtime générées une fois** : `bol/slr_locales.py`.
- **Déchiffrement xodus** (métadonnées lues une fois, tampons de 4 Mio,
  cache de blocs NTFS) : patches xodus 0012 à 0014.
- **Rafraîchissement MSA en parallèle de la préparation du préfixe** :
  `bol/launch.py:921-952`.
- **Réutilisation du pré-auth et préchauffage par la fenêtre** :
  `bol/auth.py:1045-1050`, `bol/gui.py:2940`.
- **Registre écrit hors ligne, sans démarrer Wine** : `bol/wine_registry.py`.
- **Aucune sonde Vulkan côté lanceur** : choix de sécurité GPU.
- **Cache de shaders stable entre versions** : `bol/launch.py:846-853`.
- **Cache de la licence hors ligne (#308)** : refusé, c'est un
  contournement.
- **Lancer hors du conteneur** : le réseau GDK ne marche que dans le Steam
  Linux Runtime (`bol/prefix.py:227-229`).
- **Supprimer l'archive moteur épinglée du cache (863 Mo)** : conservée
  volontairement.
- **Sauvegardes de DLL du préfixe et vieilles archives** : déjà purgées
  (#289).
- **Cache des packs de serveurs** : déjà élagué à chaque lancement
  (`bol/packcache.py`).
- **Mise à jour umu à chaque lancement** : déjà coupée
  (`UMU_RUNTIME_UPDATE=0`).
- **Deltas AppImage** : `.zsync` déjà publié (#191).
- **Gains FPS de flarialmc/linux** : comparés, rien à reprendre.
- **`WINEDEBUG=-all` hors diagnostic, et `VKD3D_DEBUG=info`** : le second
  n'écrit rien par image, à laisser.
- **GPU** : sur la machine de dev le lag vient du thread principal et de la
  RAM ; aucune piste de cet audit ne vise le GPU.

## 5. Ordre proposé

1. Q1, Q4, Q2 : trois changements de quelques lignes, sans nouvelle
   révision moteur ni xodus, qui touchent la RAM, le thread principal et le
   démarrage.
2. Q3, Q5, Q6, Q7, Q8 ensemble : une passe « un seul PLAY, une seule
   préparation », à mesurer avant et après avec le même relevé que le
   budget de lancement existant.
3. M1 avec la prochaine révision moteur, puis M4 (a).
4. M2, puis M3 avec le prochain rev xodus.
5. Les paris seulement après mesure de ce qui précède ; P1 d'abord.
