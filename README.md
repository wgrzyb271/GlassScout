# ◈ Home Lab Command Center

**A simple, elegant dashboard for opening and keeping an eye on your home lab services.**

Home Lab Command Center brings your local web interfaces together in one place. Add a service once, then open it from its own card whenever you need it. The dashboard is built with Streamlit and runs on your computer or home server.

## ✨ What you can do

- Open a service such as AdGuard Home, Proxmox, a Windows VM, or Kali Linux in a new browser tab.
- Add, edit, and remove service cards from the sidebar.
- Check whether a service's network port accepts connections.
- See the current time without refreshing the page.
- Use the dashboard on desktop, tablet, or phone.
- Enjoy a gently animated Golden Gate background with a frosted-glass interface.

## 🌗 Day and night appearance

The dashboard changes its accent color and day/night label automatically using the **local time on the computer or server running the app**:

| Local time | Appearance |
| --- | --- |
| 06:00–17:59 | Day Shift with a warm amber accent |
| 18:00–05:59 | Night Shift with a soft violet accent |

The background photo moves slowly with a subtle pan and zoom. If your device has **Reduce motion** enabled, the background animation is paused.

## 🖥️ Dashboard preview

The dashboard pairs a live service overview with a frosted-glass interface and a gently animated Golden Gate backdrop.

![Home Lab Command Center with the sidebar collapsed](readme-assets/dashboard-preview.png)

*The main dashboard with the sidebar collapsed.*

![Home Lab Command Center with the sidebar open](readme-assets/sidebar-preview.png)

*The sidebar expanded for endpoint settings and service management.*

## 🚀 Get started

### 1. Install Python

Install **Python 3.10 or newer**. On macOS or Linux, `python3 --version` should print your installed version. On Windows, use `py --version`.

### 2. Clone the repository

```bash
git clone https://github.com/wgrzyb271/GlassScout.git
cd GlassScout
```

### 3. Create an environment and install the app

#### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

#### Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 4. Start the dashboard

```bash
python -m streamlit run app.py
```

Open the local address shown in the terminal, usually <http://localhost:8501>. To stop the app, return to the terminal and press **Ctrl+C**.

## 🧩 Add your services

1. Open **Add service** in the sidebar.
2. Enter a name and URL. Category, description, icon, and accent color are optional presentation details.
3. Select **Add to dashboard**. The new card appears with the others.
4. To change a service URL later, edit it in **Service Endpoints** and select **Save endpoint changes**.
5. To remove a card, open **Manage services** and select **Remove** beside it.

Your service list is saved locally in `data/services.json` and is kept when you restart the app. Use URLs that your browser can reach, for example `http://192.168.1.20:8080` or `https://proxmox.example.local:8006`.

## 🟢 About the connection status

Turn on **Check node heartbeat** to check whether each service's host and TCP port accept a connection. `ONLINE` means the port accepted the connection; `UNREACHABLE` means it did not. This does **not** sign in, load the service's web page, or check whether the application itself is healthy. With the option off, cards show `LINK READY` instead.

## 🔭 Planned: LangChain network discovery

A future version is planned to include an on-demand LangChain agent. When started by the user, it will scan the authorized local network for reachable hosts and open service ports, gather details about active services, and use those findings to add new services to the dashboard or update existing entries. This capability is planned and is **not available in the current version**. Network discovery should only be run on networks you own or are authorized to assess.

## 📋 Requirements

- Python 3.10 or newer
- Streamlit (installed from `requirements.txt`)
- A modern web browser
- Network access during the initial dependency installation

The dashboard uses the included local photo, so it does not need an external image service. Service data is stored in a local JSON file; no database or account is required.
