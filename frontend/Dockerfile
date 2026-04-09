FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
ARG NPM_REGISTRY
RUN if [ -n "$NPM_REGISTRY" ]; then npm ci --registry "$NPM_REGISTRY"; else npm ci; fi
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /app-entrypoint.sh
RUN chmod +x /app-entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/app-entrypoint.sh"]
CMD []
