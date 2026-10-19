# OpenAI image and S3 smoke test procedure

This procedure is intentionally not part of pytest. It may call paid OpenAI APIs and create an S3 object, so an operator must obtain separate approval and use a disposable non-production bucket.

## Preconditions

- A dedicated OpenAI API key is supplied only through the environment.
- `STORAGE_PROVIDER=s3`, `S3_BUCKET` names a disposable private bucket, and `S3_REGION` is set.
- The AWS identity has only `PutObject`, `GetObject`, and `DeleteObject` for a unique smoke-test prefix. Do not use production credentials or a production bucket.
- A valid one-image source file and a workflow input with exactly five card slides are available.

## Intended minimum run after approval

1. Configure `OPENAI_API_KEY`, `STORAGE_PROVIDER=s3`, `S3_BUCKET`, and `S3_REGION` in the operator shell; never place values in code or test logs.
2. Execute one V3 workflow with one source image and five slides. `OpenAIImageProvider` uses the Image edit API with `output_format="png"`.
3. Confirm five `FileAsset` rows have `IMAGE`, expected `slide_no` values 1 through 5, `image/png`, positive `file_size`, and the expected private S3 keys.
4. Materialize each key through `S3StorageProvider.materialize_private` and verify its PNG signature and byte size. Confirm the S3 object's `ContentType` is `image/png`.
5. Run IMAGE_CHECK once with the configured Vision model and confirm its data-URI inputs contain the source image plus the five generated images.
6. Delete only the explicitly created smoke-test prefix after verification and record the request IDs outside the application logs.

No command is supplied here deliberately: it would risk accidental vendor calls. The automated suite covers the same request/response shapes with OpenAI and boto3 mocks, local PNG bytes, and S3 upload/download stubs.

## Publication boundary

The current V3 `PUBLICATION` node only persists the ordered five-card asset set. An Instagram Carousel provider is not implemented, and PENDING `SocialPublication` records do not make Instagram requests. Do not route card images to the existing Reels implementation.
