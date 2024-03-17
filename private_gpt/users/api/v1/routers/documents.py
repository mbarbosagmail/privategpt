import os
import logging
import aiofiles
import traceback
from pathlib import Path
from datetime import datetime

from typing import Any, List
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException, status, Security, Request, File, UploadFile

from private_gpt.users.api import deps
from private_gpt.constants import UNCHECKED_DIR
from private_gpt.users.constants.role import Role
from private_gpt.users import crud, models, schemas
from private_gpt.server.ingest.ingest_router import create_documents, ingest
from private_gpt.users.models.document import MakerCheckerActionType, MakerCheckerStatus
from private_gpt.components.ocr_components.table_ocr_api import process_both_ocr, process_ocr

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/documents', tags=['Documents'])


@router.get("", response_model=List[schemas.Document])
def list_files(
    request: Request,
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: models.User = Security(
        deps.get_current_user,
        scopes=[Role.ADMIN["name"], Role.SUPER_ADMIN["name"], Role.OPERATOR["name"]], 
    )
):
    """
    List the documents based on the role. 
    """
    def get_username(db, id):
        user = crud.user.get_by_id(db=db, id=id)
        return user.username

    try:
        role = current_user.user_role.role.name if current_user.user_role else None
        if (role == "SUPER_ADMIN") or (role == "OPERATOR"):
            docs = crud.documents.get_multi(db, skip=skip, limit=limit)
        else:
            docs = crud.documents.get_multi_documents(
                db, department_id=current_user.department_id, skip=skip, limit=limit)
        
        documents = [
            schemas.Document(
                id=doc.id,
                filename=doc.filename,
                uploaded_by=get_username(db, doc.uploaded_by),
                uploaded_at=doc.uploaded_at,
                is_enabled=doc.is_enabled,
                departments=[
                    schemas.DepartmentList(id=dep.id, name=dep.name)
                    for dep in doc.departments
                ]
            )
            for doc in docs
        ]
        return documents
    except Exception as e:
        print(traceback.format_exc())
        logger.error(f"There was an error listing the file(s).")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error",
        )


@router.get('{department_id}', response_model=List[schemas.DocumentList])
def list_files_by_department(
    request: Request,
    department_id: int,
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: models.User = Security(
        deps.get_current_user,
        scopes=[Role.SUPER_ADMIN["name"]],
    )
):
    '''
    Listing the documents by the department id
    '''
    try:
        docs = crud.documents.get_documents_by_departments(
            db, department_id=department_id, skip=skip, limit=limit)
        return docs
    except Exception as e:
        print(traceback.format_exc())
        logger.error(f"There was an error listing the file(s).")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error.",
        )


@router.get('/files', response_model=List[schemas.DocumentList])
def list_files_by_department(
    request: Request,
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: models.User = Security(
        deps.get_current_user,
        scopes=[Role.ADMIN["name"], Role.SUPER_ADMIN["name"], Role.OPERATOR["name"]], 
    )
):
    '''
    Listing the documents by the ADMIN of the Department
    '''
    try:
        department_id = current_user.department_id
        docs = crud.documents.get_documents_by_departments(
            db, department_id=department_id, skip=skip, limit=limit)
        return docs
    except Exception as e:
        print(traceback.format_exc())
        logger.error(f"There was an error listing the file(s).")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error.",
        )


@router.post('/update', response_model=schemas.DocumentEnable)
def update_document(
    request: Request,
    document_in: schemas.DocumentEnable ,
    db: Session = Depends(deps.get_db),
    log_audit: models.Audit = Depends(deps.get_audit_logger),
    current_user: models.User = Security(
        deps.get_current_user,
        scopes=[Role.SUPER_ADMIN["name"], Role.OPERATOR["name"]], 
    )
):
    '''
    Function to enable or disable document.
    '''
    try:
        document = crud.documents.get_by_filename(
            db, file_name=document_in.filename)
        if not document:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document with this filename doesn't exist!",
            )
        docs = crud.documents.update(db=db, db_obj=document, obj_in=document_in)
        log_audit(
            model='Document', 
            action='update',
            details={
                'detail': f'{document_in.filename} status changed to {document_in.is_enabled} from {document.is_enabled}'
            }, 
            user_id=current_user.id
        )
        return docs
    except Exception as e:
        print(traceback.format_exc())
        logger.error(f"There was an error listing the file(s).")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error.",
        )
    

@router.post('/department_update', response_model=schemas.DocumentList)
def update_department(
    request: Request,
    document_in: schemas.DocumentDepartmentUpdate,
    db: Session = Depends(deps.get_db),
    log_audit: models.Audit = Depends(deps.get_audit_logger),
    current_user: models.User = Security(
        deps.get_current_user,
        scopes=[Role.SUPER_ADMIN["name"], Role.OPERATOR["name"]], 
    )
):
    """
    Update the department list for the documents
    """
    try:
        document = crud.documents.get_by_filename(
            db, file_name=document_in.filename)
        old_departments = document.departments
        if not document:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document with this filename doesn't exist!",
            )
        department_ids = [int(number) for number in document_in.departments]
        for department_id in department_ids:
            db.execute(models.document_department_association.insert().values(document_id=document.id, department_id=department_id))
        log_audit(
            model='Document', 
            action='update',
            details={
                'detail': f'{document_in.filename} assigned to {department_ids} from {old_departments}'
            }, 
            user_id=current_user.id
        )
        return document
    except Exception as e:
        print(traceback.format_exc())
        logger.error(f"There was an error listing the file(s).")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error.",
        )
    

@router.post('/upload', response_model=schemas.Document)
async def upload_documents(
    request: Request,
    departments: schemas.DocumentDepartmentList = Depends(),
    file: UploadFile = File(...),

    log_audit: models.Audit = Depends(deps.get_audit_logger),
    db: Session = Depends(deps.get_db),
    current_user: models.User = Security(
        deps.get_current_user,
        scopes=[Role.ADMIN["name"],
                Role.SUPER_ADMIN["name"], 
                Role.OPERATOR["name"]],
    )
):
    """Upload the documents."""
    try:
        original_filename = file.filename
        if original_filename is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No file name provided",
            )
        upload_path = Path(f"{UNCHECKED_DIR}/{original_filename}")
        try:
            contents = await file.read()
            async with aiofiles.open(upload_path, 'wb') as f:
                await f.write(contents)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal Server Error: Unable to ingest file.",
            )
        document = await create_documents(db, original_filename, current_user, departments, log_audit)
        logger.info(
            f"{original_filename} is uploaded by {current_user.username} in {departments.departments_ids}")
        return document

    except HTTPException:
        print(traceback.print_exc())
        raise

    except Exception as e:
        print(traceback.print_exc())
        logger.error(f"There was an error uploading the file(s): {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal Server Error: Unable to upload file.",
        )


@router.post('/verify')
async def verify_documents(
    request: Request,
    checker_in: schemas.DocumentUpdate = Depends(),
    log_audit: models.Audit = Depends(deps.get_audit_logger),
    db: Session = Depends(deps.get_db),
    current_user: models.User = Security(
        deps.get_current_user,
        scopes=[Role.ADMIN["name"],
                Role.SUPER_ADMIN["name"],
                Role.OPERATOR["name"]],
    )
):
    """Upload the documents."""
    try:
        document = crud.documents.get_by_id(db, id=checker_in.id)
        if not document:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Document not found!",
            )
        unchecked_path = Path(f"{UNCHECKED_DIR}/{document.filename}")

        if checker_in.status == MakerCheckerStatus.APPROVED.value:
            checker = schemas.DocumentCheckerUpdate(
                    status=MakerCheckerStatus.APPROVED,
                    is_enabled=checker_in.is_enabled,
                    verified_at=datetime.now(),
                    verified_by=current_user.id,
                )
            crud.documents.update(db=db, db_obj= document, obj_in=checker)

            if document.doc_type_id == 2:
                return await process_ocr(request, unchecked_path)
            elif document.doc_type_id == 3:
                return await process_both_ocr(request, unchecked_path)
            else:
                return await ingest(request, unchecked_path)
            
        elif checker_in.status == MakerCheckerStatus.REJECTED.value:
            checker = schemas.DocumentCheckerUpdate(
                status=MakerCheckerStatus.REJECTED,
                is_enabled=checker_in.is_enabled,
                verified_at=datetime.now(),
                verified_by=current_user.id,
            )
            crud.documents.update(db=db, db_obj=document, obj_in=checker)
            os.remove(unchecked_path)

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change status to PENDING!",
            )

    except HTTPException:
        print(traceback.print_exc())
        raise

    except Exception as e:
        print(traceback.print_exc())
        logger.error(f"There was an error uploading the file(s): {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal Server Error: Unable to upload file.",
        )
