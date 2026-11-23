FROM mcr.microsoft.com/playwright:v1.58.2-noble
WORKDIR /browser
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ENV BASE_URL=http://localhost:4173
CMD ["npm", "test"]
