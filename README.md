# To unzip locally
unzip be-the-electron.zip -d agentic-ai-projects
cd agentic-ai-projects
git init -b main
git add .
git commit -m "Add Be the Electron POC"
git remote add origin https://github.com/niranjani23/agentic-ai-projects.git
git push -u origin main

# To execute the app
cd hackathon/be-the-electron
npm install
cp .env.local.example .env.local   # paste your GEMINI_API_KEY
npm run dev                        # http://localhost:3000
