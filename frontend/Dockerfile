FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY src/env.template.js /usr/share/nginx/html/env.template.js
COPY docker-entrypoint.sh /docker-entrypoint.sh

# non root user, it cant expose any of the 1 to 1023
USER app

# permissions to file to stay away from rootuser
RUN chmod +x /docker-entrypoint.sh && \
    chown -R app:app /usr/share/nginx/html /var/cache/nginx /var/log/nginx /var/run && \
    touch /var/run/nginx.pid && \
    chown app:app /var/run/nginx.pid

# Add this right above the USER app line
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:8080/ || exit 1

EXPOSE 8080
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
