FROM node:22.15.0-alpine3.21 AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27.4-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY proxy-params.conf /etc/nginx/snippets/proxy-params.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
