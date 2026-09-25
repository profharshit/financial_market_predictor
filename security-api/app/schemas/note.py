from pydantic import BaseModel, Field


class NoteCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class NoteResponse(BaseModel):
    id: int
    user_id: int
    content: str

    model_config = {
        "from_attributes": True
    }