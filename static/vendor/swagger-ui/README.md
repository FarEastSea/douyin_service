# Swagger UI

The API documentation serves Swagger UI 5.34.1 from this directory so it works
without a third-party CDN. These files are copied unchanged from the official
`swagger-ui-dist` npm package. Keep its license and bundled dependency notices.

Source: https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.34.1.tgz

Package integrity (SHA-512, base64):
`VSr8fXfI93obYaQicLJbLUKGRxDojj/SfRJDDLCioGTUBj4BK/Nj4eE9jCeCAnJxVLi7iS+08PbiZLyrQKBkJQ==`

To upgrade, copy the bundle, CSS, favicon and license files into a new versioned
directory, then update the three asset URLs in `main.py`. These assets live
outside `static/app` so rebuilding the frontend does not remove them.
