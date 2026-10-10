# Wdrożenie dokładniejszego skanowania na LXC 101

Paczka zawiera kod i dokumentację. Nie zawiera kluczy API ani danych dashboardu.
Przed wdrożeniem zakończ aktywny skan. Zmiany są lokalne; samo `git pull` ich nie
pobierze, dopóki nie zostaną zatwierdzone i wysłane do zdalnego repozytorium.

Na świeżym kontenerze Debian cały etap instalacji można wykonać skryptem:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/wgrzyb271/GlassScout.git
cd GlassScout
bash scripts/setup_proxmox_lxc.sh
```

Opcja `--with-browser` instaluje również Playwright i Chromium. Skrypt tworzy
użytkownika, środowisko Python, usługę uruchamianą przy starcie LXC oraz
szyfrowany magazyn kluczy. Dalsze kroki dotyczą aktualizacji istniejącego LXC.

## 1. Terminal Maca

```bash
scp /private/tmp/glassscout-discovery-upgrade.tar.gz root@192.168.2.90:/tmp/
```

## 2. Proxmox: pve → Shell

```bash
pct push 101 /tmp/glassscout-discovery-upgrade.tar.gz /tmp/glassscout-discovery-upgrade.tar.gz
```

## 3. Konsola kontenera 101

Najpierw zatrzymaj aplikację i wykonaj kopię kodu:

```bash
cd /opt/glassscout/app
systemctl stop glassscout
tar -czf /opt/glassscout/discovery-before-upgrade.tar.gz discovery dashboard/agent_panel.py dashboard/discovery_progress.py
```

Wgraj nowe pliki i uruchom aplikację:

```bash
tar -xzf /tmp/glassscout-discovery-upgrade.tar.gz
chown -R glassscout:glassscout discovery
chown glassscout:glassscout dashboard/agent_panel.py requirements-browser.txt
systemctl start glassscout
systemctl status glassscout --no-pager
```

Rozszerzone HTTP, rozpoznawanie znanych usług, Nmap i kolejka działają bez
instalowania przeglądarki.

## 4. Opcjonalny etap JavaScript

W kontenerze jako root wykonaj po kolei (przy błędzie zatrzymaj się):

```bash
runuser -u glassscout -- /opt/glassscout/app/.venv/bin/pip install -r /opt/glassscout/app/requirements-browser.txt
/opt/glassscout/app/.venv/bin/python -m playwright install-deps chromium
runuser -u glassscout -- /opt/glassscout/app/.venv/bin/python -m playwright install chromium
systemctl restart glassscout
```

Chromium pobierane jest na konto użytkownika aplikacji. Systemd ma już ustawione
`HOME=/opt/glassscout`. Przeglądarka wymaga działającego sandboxa; jeśli LXC nie
pozwala jej uruchomić, skaner zachowa wyniki HTTP i zapisze komunikat w aktywności.

## 5. Test routera bez skanowania całej sieci

Otwórz http://dashboard.home:8501 i wybierz **Discover services**:

- Odznacz podsieci i usuń opcjonalną dodatkową podsieć.
- W **Known service URLs** wpisz `http://192.168.2.1/`.
- Ustaw limit 5 minut.
- W **Advanced scan settings** zaznacz **Render unresolved pages with a browser**
  po zainstalowaniu Chromium.
- Uruchom skan i sprawdź aktywność oraz **Needs review**.

Włączenie przeglądarki nie gwarantuje identyfikacji każdego routera. Nazwa
oparta na jednej stronie pozostanie propozycją do ręcznego zatwierdzenia.
Automatyczne dodanie wymaga odrębnych, zgodnych dowodów i ponownej weryfikacji.
Dla znanego firmware takim drugim dowodem może być osobny zasób firmware albo
ponownie sprawdzone przekierowanie z adresu routera do oznaczonego panelu.

Po przerwaniu nowego skanu opcja **Resume remaining addresses** pozwala wznowić
zapisane adresy bez ponownego skanowania portów. Starszy raport sprzed tej zmiany
nie zawiera kolejki i nie można go w ten sposób wznowić.

## 6. Szyfrowane klucze API w systemd

Panel nie zawiera pola klucza API. Aplikacja odczytuje sekret z poświadczenia
systemd dostępnego tylko podczas pracy usługi. W typowym LXC bez TPM używany
jest klucz hosta zapisany jako plik dostępny wyłącznie dla roota. Chroni to
sekret przechowywany na dysku, ale root w tym samym kontenerze nadal może go
odszyfrować.

W konsoli LXC jako root zatrzymaj usługę i utwórz magazyn:

```bash
systemctl stop glassscout
install -d -m 0700 /etc/credstore.encrypted
systemd-creds setup
```

`systemd-creds setup` tworzy losowy klucz hosta w
`/var/lib/systemd/credential.secret`. Następne polecenie pyta o klucz Groq bez
wyświetlania znaków i zapisuje tylko zaszyfrowany, uwierzytelniony plik:

```bash
systemd-ask-password -n "Groq API key:" | systemd-creds encrypt --with-key=host --name=GROQ_API_KEY - /etc/credstore.encrypted/GROQ_API_KEY
chmod 0600 /etc/credstore.encrypted/GROQ_API_KEY
```

Jeżeli używasz Gemini, wykonaj także:

```bash
systemd-ask-password -n "Gemini API key:" | systemd-creds encrypt --with-key=host --name=GEMINI_API_KEY - /etc/credstore.encrypted/GEMINI_API_KEY
chmod 0600 /etc/credstore.encrypted/GEMINI_API_KEY
```

Dodaj plik rozszerzający usługę:

```bash
mkdir -p /etc/systemd/system/glassscout.service.d
nano /etc/systemd/system/glassscout.service.d/credentials.conf
```

Wklej do niego:

```ini
[Service]
LoadCredentialEncrypted=GROQ_API_KEY
LoadCredentialEncrypted=GEMINI_API_KEY
```

Nazwy bez ścieżek każą systemd szukać w `/etc/credstore.encrypted`. Brak klucza
drugiego dostawcy nie zatrzyma usługi. Następnie otwórz konfigurację Streamlit:

```bash
nano /opt/glassscout/app/.streamlit/secrets.toml
```

Usuń z niej wszystkie linie `GROQ_API_KEY`, `GEMINI_API_KEY` i `GOOGLE_API_KEY`.
Zostaw tylko niesekretne ustawienia, na przykład:

```toml
LLM_PROVIDER = "groq"
GROQ_MODEL = "openai/gpt-oss-120b"
GEMINI_MODEL = "gemini-3.6-flash"
```

Na końcu przeładuj konfigurację i sprawdź usługę:

```bash
systemctl daemon-reload
systemctl restart glassscout
systemctl status glassscout --no-pager
systemctl show glassscout -p LoadCredentialEncrypted
```

Ostatnie polecenie pokazuje tylko nazwy załadowanych poświadczeń, bez ich
wartości. W oknie skanowania pojawi się jedynie informacja, że klucz wybranego
dostawcy jest skonfigurowany na serwerze.

## Cofnięcie kodu

```bash
cd /opt/glassscout/app
systemctl stop glassscout
tar -xzf /opt/glassscout/discovery-before-upgrade.tar.gz
systemctl start glassscout
```

Kopia dotyczy kodu. Nie cofa nowych kart dodanych podczas skanowania.
