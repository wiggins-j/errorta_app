import { useCallback } from "react";
import { useDropzone } from "react-dropzone";

export default function FileUploadArea({
  onFiles,
  supportedExtensions,
  disabled,
}: {
  onFiles: (files: File[]) => void;
  supportedExtensions: string[];
  disabled?: boolean;
}) {
  const onDrop = useCallback(
    (accepted: File[]) => {
      if (accepted.length > 0) onFiles(accepted);
    },
    [onFiles],
  );
  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    disabled,
    multiple: true,
    noClick: false,
  });
  return (
    <div
      {...getRootProps()}
      className={`dropzone ${isDragActive ? "dropzone-active" : ""}`}
      aria-label="Drop files here or click to browse"
    >
      <input {...getInputProps({ "aria-label": "Choose files to ingest" })} />
      <p>
        <strong>Drop files here</strong> or click to browse.
      </p>
      <p className="dropzone-hint">
        Supported: {supportedExtensions.join(", ") || "—"}
      </p>
    </div>
  );
}
